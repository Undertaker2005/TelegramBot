from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from telegram_bot.config import Settings
from telegram_bot.content import parse_course_json
from telegram_bot.database import Database
from telegram_bot.logic import (
    learning_plan,
    next_lesson,
    placement_breakdown,
    reminder_is_due,
    review_passed,
    score_placement,
    study_streak,
    validate_reminder_time,
    validate_timezone,
)
from telegram_bot.open_dataset import OpenDataset, normalize_lookup
from telegram_bot.reference_data import CEFR_LEVELS, ReferenceLibrary

logger = logging.getLogger(__name__)
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Продовжити урок"), KeyboardButton(text="Повторити слова")],
        [KeyboardButton(text="Мій прогрес"), KeyboardButton(text="Мій план")],
        [KeyboardButton(text="Налаштування")],
    ],
    resize_keyboard=True,
)
DAY_PRESETS = {
    "daily": list(range(7)),
    "weekdays": list(range(5)),
    "weekends": [5, 6],
}
DAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
CEFR_DESCRIPTIONS = {
    "A1": "Початковий: прості знайомі слова й короткі фрази.",
    "A2": "Базовий: повсякденні теми й прості типові ситуації.",
    "B1": "Середній: основні думки знайомих тем і зв'язне спілкування.",
    "B2": "Вище середнього: складніші тексти й аргументація на знайомі теми.",
    "C1": "Просунутий: складні тексти та гнучке доречне мовлення.",
    "C2": "Майже вільне володіння: розуміння й точне використання складної мови.",
}
PART_OF_SPEECH_UK = {
    "noun": "іменник",
    "verb": "дієслово",
    "adjective": "прикметник",
    "adverb": "прислівник",
    "determiner": "визначник",
    "preposition": "прийменник",
    "conjunction": "сполучник",
    "pronoun": "займенник",
    "interjection": "вигук",
    "article": "артикль",
    "numeral": "числівник",
    "modal verb": "модальне дієслово",
}
LEVEL_SHORT_NAMES = {
    "A1": "початковий",
    "A2": "базовий",
    "B1": "середній",
    "B2": "вище середнього",
    "C1": "просунутий",
    "C2": "майже вільний",
}


def _part_of_speech_uk(value: str) -> str:
    return PART_OF_SPEECH_UK.get(value.casefold(), value or "не вказана")


def _level_keyboard() -> InlineKeyboardMarkup:
    return _inline(
        [
            [
                (f"{level} · {LEVEL_SHORT_NAMES[level]}", f"level:{level}")
                for level in CEFR_LEVELS[start : start + 2]
            ]
            for start in range(0, len(CEFR_LEVELS), 2)
        ]
    )


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def _state_data(user: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(user["state_data_json"])
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_exercise_answer(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized.strip(" \t\r\n.,!?;:\"'“”‘’()[]{}")


def _shuffled_option_order(
    options: list[str], correct_index: int, previous_correct_position: int | None = None
) -> tuple[list[int], int]:
    if not 0 <= correct_index < len(options):
        raise ValueError("Correct option index is outside the options list.")
    order = list(range(len(options)))
    random.shuffle(order)
    correct_position = order.index(correct_index)
    if len(order) > 1 and correct_position == previous_correct_position:
        replacement = (correct_position + random.randrange(1, len(order))) % len(order)
        order[correct_position], order[replacement] = order[replacement], order[correct_position]
        correct_position = replacement
    return order, correct_position


async def _touch(db: Database, telegram_id: int) -> dict[str, Any] | None:
    user = await db.get_user(telegram_id)
    if user:
        await db.update_user(telegram_id)
        user = await db.get_user(telegram_id)
    return user


async def _get_course(db: Database, user: dict[str, Any]) -> dict[str, Any] | None:
    if not user.get("course_id"):
        return None
    return await db.get_published_course(user["course_id"])


async def _show_home(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user["privacy_accepted"]:
        await message.answer("Щоб продовжити, запустіть /start і підтвердьте повідомлення про приватність.")
        return
    await message.answer(
        "Головне меню. Оберіть наступний крок або скористайтеся командами /help, /progress чи /settings.",
        reply_markup=MAIN_MENU,
    )


async def _send_courses(message: Message, db: Database) -> None:
    courses = await db.published_courses()
    if not courses:
        await message.answer("Зараз немає опублікованих курсів. Спробуйте пізніше або напишіть /support.")
        return
    rows = [[(course["title"], f"course:{course['id']}")] for course in courses]
    await message.answer("Оберіть напрямок навчання:", reply_markup=_inline(rows))


def _all_words(course: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        word["id"]: word
        for lesson in course["lessons"]
        for word in lesson["vocabulary"]
    }


def _chunks(text: str, limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        while len(candidate) > limit:
            chunks.append(candidate[:limit])
            candidate = candidate[limit:]
        current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


async def _send_lesson(
    message: Message, db: Database, course: dict[str, Any], lesson: dict[str, Any], telegram_id: int
) -> None:
    words = lesson["vocabulary"]
    await db.add_vocabulary(telegram_id, course["id"], words)
    header = (
        f"Урок {lesson['order']}: {lesson['title']}\n"
        f"Мета: {lesson['objectives']}\n\n{lesson['explanation']}"
    )
    if lesson.get("video"):
        header += f"\n\nВідео: {lesson['video']['title']}\n{lesson['video']['url']}"
    else:
        header += "\n\nВідео до цього уроку ще не додано модератором."
    for chunk in _chunks(header):
        await message.answer(chunk, reply_markup=MAIN_MENU)
    if words:
        vocabulary = "Слова уроку:\n" + "\n\n".join(
            f"{word['word']} — {word['translation']}"
            f"{' (' + word['transcription'] + ')' if word.get('transcription') else ''}\n"
            f"{word.get('part_of_speech', '')} · {word['example']}\n{word['example_translation']}"
            for word in words
        )
        for chunk in _chunks(vocabulary):
            await message.answer(chunk)
    await _begin_exercise(message, db, course, lesson, 0, telegram_id)


async def _begin_exercise(
    message: Message,
    db: Database,
    course: dict[str, Any],
    lesson: dict[str, Any],
    exercise_index: int,
    telegram_id: int,
    attempts: int = 0,
) -> None:
    if exercise_index >= len(lesson["exercises"]):
        await db.complete_lesson(telegram_id, course["id"], lesson["id"])
        await db.set_state(telegram_id, None)
        await message.answer(
            f"Урок «{lesson['title']}» завершено. Наступного разу перевіримо слова, які час повторити.",
            reply_markup=MAIN_MENU,
        )
        return
    exercise = lesson["exercises"][exercise_index]
    user = await db.get_user(telegram_id)
    previous_data = _state_data(user or {})
    options = exercise.get("options", [])
    display_order: list[int] = []
    correct_position: int | None = None
    if options:
        correct_index = options.index(exercise["correct_answer"])
        if (
            previous_data.get("course_id") == course["id"]
            and previous_data.get("lesson_id") == lesson["id"]
            and previous_data.get("exercise_index") == exercise_index
            and sorted(previous_data.get("display_order", [])) == list(range(len(options)))
        ):
            display_order = previous_data["display_order"]
            correct_position = display_order.index(correct_index)
        else:
            previous_position = (
                previous_data.get("correct_position")
                if previous_data.get("course_id") == course["id"]
                and previous_data.get("lesson_id") == lesson["id"]
                and previous_data.get("exercise_index") == exercise_index - 1
                else None
            )
            display_order, correct_position = _shuffled_option_order(
                options, correct_index, previous_position
            )
    data = {
        "course_id": course["id"],
        "lesson_id": lesson["id"],
        "exercise_index": exercise_index,
        "attempts": attempts,
        "display_order": display_order,
        "correct_position": correct_position,
    }
    await db.set_state(telegram_id, "exercise", data)
    if exercise.get("answer_mode") == "text":
        await message.answer(
            f"Вправа {exercise_index + 1}/{len(lesson['exercises'])}:\n"
            f"{exercise['sentence']}\nВведіть відповідь повідомленням.",
            reply_markup=_inline([[("Пропустити вправу", f"exskip:{exercise_index}")]]),
        )
        return
    rows = [[
        (exercise["options"][index], f"ex:{exercise_index}:{index}")
        for index in display_order
    ]]
    rows.append([("Пропустити вправу", f"exskip:{exercise_index}")])
    await message.answer(
        f"Вправа {exercise_index + 1}/{len(lesson['exercises'])}:\n"
        f"{exercise['sentence'].replace('____', '_____')}",
        reply_markup=_inline(rows),
    )


async def _begin_lesson(
    message: Message,
    db: Database,
    settings: Settings,
    telegram_id: int,
    *,
    skip_review: bool = False,
) -> None:
    user = await db.get_user(telegram_id)
    if not user or not user["privacy_accepted"]:
        await message.answer("Спершу запустіть /start.")
        return
    if user["paused"]:
        await message.answer("Навчання призупинено. Щоб продовжити, надішліть /resume.")
        return
    course = await _get_course(db, user)
    if not course:
        await _send_courses(message, db)
        return
    if user["state"] == "review_result":
        await message.answer(
            "Спершу завершіть повторний тест слів, щоб відкрити наступний урок.",
            reply_markup=_inline([[("Повторити тест", "review:retry")]]),
        )
        return
    if user["state"] == "review":
        saved = _state_data(user)
        if saved.get("course_id") == course["id"]:
            index = saved.get("review_index")
            word_ids = saved.get("word_ids", [])
            if index == len(word_ids) and word_ids:
                await _finish_review(message, db, settings, course, telegram_id, saved)
                return
            if isinstance(index, int) and 0 <= index < len(word_ids) and word_ids[index] in _all_words(course):
                await message.answer("Продовжуємо тест повторення.")
                await _show_review_question(message, db, course, word_ids, index, telegram_id)
                return
        await db.set_state(telegram_id, None)
    if user["state"] == "exercise":
        saved = _state_data(user)
        if saved.get("course_id") == course["id"]:
            saved_lesson = next(
                (item for item in course["lessons"] if item["id"] == saved.get("lesson_id")), None
            )
            index = saved.get("exercise_index")
            if saved_lesson and isinstance(index, int) and 0 <= index < len(saved_lesson["exercises"]):
                await message.answer(f"Продовжуємо вправу уроку «{saved_lesson['title']}».")
                await _begin_exercise(
                    message, db, course, saved_lesson, index, telegram_id, int(saved.get("attempts", 0))
                )
                return
        await db.set_state(telegram_id, None)
    completed = await db.completed_lessons(telegram_id, course["id"])
    level = user.get("level")
    if level not in CEFR_LEVELS:
        await message.answer("Спершу оберіть рівень командою /level.")
        return
    available_lessons = [
        item for item in course["lessons"] if item["level"] == level
    ]
    lesson = next_lesson(available_lessons, completed)
    if not lesson:
        if not available_lessons:
            await message.answer(
                f"У курсі поки немає опублікованих уроків рівня {level}. "
                "Матеріал іншого рівня не буде підставлено; скористайтеся /open_practice."
            )
        else:
            await message.answer(
                f"Ви завершили всі доступні уроки рівня {level}. Нові матеріали з'являться після модерації."
            )
        return
    if completed and not skip_review:
        due_ids = await db.due_vocabulary(telegram_id, course["id"])
        if due_ids:
            await _begin_review(message, db, settings, course, due_ids, telegram_id)
            return
    await _send_lesson(message, db, course, lesson, telegram_id)


async def _begin_review(
    message: Message,
    db: Database,
    settings: Settings,
    course: dict[str, Any],
    word_ids: list[str],
    telegram_id: int,
) -> None:
    words = _all_words(course)
    valid_ids = [word_id for word_id in word_ids if word_id in words]
    if not valid_ids:
        await _begin_lesson(message, db, settings, telegram_id, skip_review=True)
        return
    await db.set_state(
        telegram_id,
        "review",
        {
            "course_id": course["id"],
            "word_ids": valid_ids,
            "review_index": 0,
            "correct": 0,
            "review_wrong_ids": [],
        },
    )
    await message.answer("Перед наступним уроком перевірмо слова з попередніх занять.")
    await _show_review_question(message, db, course, valid_ids, 0, telegram_id)


async def _show_review_question(
    message: Message,
    db: Database,
    course: dict[str, Any],
    word_ids: list[str],
    index: int,
    telegram_id: int,
) -> None:
    words = _all_words(course)
    word = words[word_ids[index]]
    distractors = list(dict.fromkeys(
        item["translation"] for key, item in words.items() if key != word["id"] and item["translation"] != word["translation"]
    ))
    random.Random(f"{word['id']}:{index}").shuffle(distractors)
    options = [word["translation"], *distractors[:2]]
    if len(options) < 3:
        options.extend(["не знаю", "потрібно повторити"])
    options = list(dict.fromkeys(options))
    random.Random(f"{word['id']}:{index}:options").shuffle(options)
    user = await db.get_user(telegram_id)
    data = _state_data(user or {})
    data["review_options"] = options
    await db.set_state(telegram_id, "review", data)
    rows = [[(option, f"rv:{index}:{choice}") for choice, option in enumerate(options)]]
    await message.answer(f"Що означає «{word['word']}»?", reply_markup=_inline(rows))


async def _finish_review(
    message: Message,
    db: Database,
    settings: Settings,
    course: dict[str, Any],
    telegram_id: int,
    data: dict[str, Any] | None = None,
) -> None:
    if data is None:
        user = await db.get_user(telegram_id)
        data = _state_data(user or {})
    word_ids = data["word_ids"]
    correct = data["correct"]
    passed = review_passed(correct, len(word_ids), settings.vocabulary_pass_threshold)
    if passed:
        await db.set_state(telegram_id, None)
        await message.answer(f"Чудово! Правильних відповідей: {correct}/{len(word_ids)}. Відкриваю наступний урок.")
        await _begin_lesson(message, db, settings, telegram_id, skip_review=True)
        return
    await db.set_state(telegram_id, "review_result", data)
    words = _all_words(course)
    await message.answer(
        f"Правильних відповідей: {correct}/{len(word_ids)}. Повторіть слова, які потребують уваги:\n"
        + "\n".join(
            f"• {words[word_id]['word']} — {words[word_id]['translation']}"
            for word_id in data.get("review_wrong_ids", word_ids)
        )
        + "\n\nПісля повторення пройдіть короткий тест ще раз.",
        reply_markup=_inline([[("Повторити тест", "review:retry")]]),
    )


def register_handlers(
    dp: Dispatcher,
    db: Database,
    settings: Settings,
    reference: ReferenceLibrary | None = None,
    open_dataset: OpenDataset | None = None,
) -> None:
    router = Router()
    reference = reference or ReferenceLibrary.empty()
    open_dataset = open_dataset or OpenDataset(
        Path(__file__).resolve().parents[1] / "data" / "open_training.sqlite3"
    )

    async def restrict_to_allowed_users(handler: Any, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        if settings.allowed_user_ids and event.from_user.id not in settings.allowed_user_ids:
            if isinstance(event, Message):
                await event.answer("Цей бот доступний лише власнику.")
            else:
                await event.answer("Цей бот доступний лише власнику.", show_alert=True)
            return None
        return await handler(event, data)

    async def reference_user(message: Message) -> tuple[dict[str, Any], str] | None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return None
        level = user.get("level")
        if level not in CEFR_LEVELS:
            await message.answer("Спершу оберіть рівень командою /level.")
            return None
        return user, level

    def reference_unavailable(filename: str | None, label: str) -> str:
        if filename and reference.read_error(filename):
            return f"Не вдалося прочитати файл {filename}. Перевірте його та запустіть бота знову."
        return (
            f"У завантажених даних немає {label}. Завантажте набори за інструкцією в README.md."
        )

    def open_dataset_unavailable() -> str:
        return (
            "Локальний відкритий словник ще не зібрано. Запустіть "
            "`scripts\\download_cefr_datasets.ps1`, а потім "
            "`python scripts\\build_open_dataset.py`."
        )

    def open_task_keyboard(
        task: dict[str, Any],
        index: int,
        mode: str,
        display_order: list[int] | None = None,
    ) -> InlineKeyboardMarkup:
        rows: list[list[tuple[str, str]]] = []
        if task["answer_mode"] == "choice":
            display_order = display_order or list(range(len(task["options"])))
            for start in range(0, len(display_order), 2):
                rows.append([
                    (
                        str(display_position + 1),
                        f"od:{index}:{display_order[display_position]}",
                    )
                    for display_position in range(
                        start, min(start + 2, len(display_order))
                    )
                ])
        if mode != "exam":
            rows.append([("Підказка", f"odh:{index}")])
        rows.append([("Пропустити", f"odskip:{index}")])
        return _inline(rows)

    async def show_open_task(
        message: Message, telegram_id: int, state: dict[str, Any]
    ) -> None:
        if state["mode"] == "exam":
            task = state.get("current_task")
        else:
            try:
                task = open_dataset.task(state["task_id"])
            except (OSError, ValueError, sqlite3.Error):
                logger.exception("Could not read the open dataset task.")
                await db.set_state(telegram_id, None)
                await message.answer(
                    "Помилка читання навчальної бази. Прохід скасовано; перевірте файли набору."
                )
                return
        if task is None:
            await db.set_state(telegram_id, None)
            await message.answer("Завдання відсутнє в локальній базі. Прохід скасовано; перевірте імпорт.")
            return
        mode = state["mode"]
        question_number = state["answered"] + 1
        label = "Іспит" if mode == "exam" else "Повтор помилок" if mode == "mistakes" else "Практика"
        level = task["level"] or "невідомий CEFR"
        prompt = (
            f"{label} {question_number}/{state['session_limit']} · рівень {level}\n"
            f"{task['prompt']}"
        )
        if task["options"]:
            display_order = state.get(
                "display_order", list(range(len(task["options"])))
            )
            prompt += "\n\n" + "\n".join(
                f"{display_position + 1}. {task['options'][option_index]}"
                for display_position, option_index in enumerate(display_order)
            )
        if mode == "exam":
            try:
                minutes_left = max(
                    0,
                    int(
                        (
                            datetime.fromisoformat(state["deadline"]) - datetime.now(UTC)
                        ).total_seconds() // 60
                    ),
                )
            except (KeyError, ValueError):
                await db.set_state(telegram_id, None)
                await message.answer("Не вдалося відновити таймер іспиту; прохід скасовано.")
                return
            prompt += (
                f"\n\nЗалишилось приблизно {minutes_left} хв. "
                "Тренувальний блок, не офіційний тест. Відповіді будуть після завершення."
            )
        elif task["level"] is None:
            prompt += "\n\nРівень складності в джерелі не зазначено."
        else:
            prompt += "\n\nПозначку рівня взято з CEFR-J для відповідного слова й частини мови."
        await message.answer(
            prompt,
            reply_markup=open_task_keyboard(
                task, question_number, mode, state.get("display_order")
            ),
        )

    async def finish_open_exam(
        message: Message, telegram_id: int, state: dict[str, Any], *, timed_out: bool = False
    ) -> None:
        answers = state.get("answers", [])
        for result in answers:
            await db.record_open_practice_attempt(
                telegram_id, result["task_id"], result["type"], result["level"],
                result["correct"], False, result.get("topic"),
            )
        await db.set_state(telegram_id, None)
        total = len(answers)
        correct = sum(bool(item["correct"]) for item in answers)
        text = (
            f"Тренувальний іспитовий блок завершено: {correct}/{total} правильних відповідей."
            if not timed_out
            else f"Час вичерпано: зараховано {correct}/{total} правильних відповідей."
        )
        explanations = []
        for result in answers:
            if result["correct"]:
                continue
            answer = ", ".join(result["correct_answers"][:4])
            explanations.append(
                f"• {result['prompt']}\nВідповідь: {answer}\nПояснення: {result['explanation']}"
            )
        if explanations:
            text += "\n\nРозбір помилок:\n" + "\n\n".join(explanations)
        sources = {
            (item["source_id"], item["license"], item["attribution"])
            for item in answers
        }
        if sources:
            text += "\n\nДжерела:\n" + "\n".join(
                f"• {source_id} · {license} · {attribution}"
                for source_id, license, attribution in sorted(sources)
            )
        text += "\n\nЦе авторське тренування, не офіційний матеріал НМТ/ЗНО."
        chunks = _chunks(text)
        for chunk in chunks[:-1]:
            await message.answer(chunk)
        await message.answer(chunks[-1], reply_markup=MAIN_MENU)

    async def start_next_open_task(
        message: Message, telegram_id: int, state: dict[str, Any]
    ) -> None:
        if state["answered"] >= state["session_limit"]:
            if state["mode"] == "exam":
                await finish_open_exam(message, telegram_id, state)
            else:
                await db.set_state(telegram_id, None)
                total = state["answered"]
                correct = state.get("correct", 0)
                await message.answer(
                    f"Блок завершено: {correct}/{total} правильних відповідей. "
                    "Статистика збережена лише у вашому профілі. Для нового проходу надішліть /open_practice.",
                    reply_markup=MAIN_MENU,
                )
            return

        if state["mode"] == "mistakes":
            position = state["mistake_position"]
            task_ids = state["task_ids"]
            if position >= len(task_ids):
                await db.set_state(telegram_id, None)
                await message.answer(
                    f"Повтор помилок завершено: {state.get('correct', 0)}/{state['answered']} правильних.",
                    reply_markup=MAIN_MENU,
                )
                return
            task_id = task_ids[position]
            try:
                task = open_dataset.task(task_id)
            except (OSError, ValueError, sqlite3.Error):
                logger.exception("Could not read a repeated open dataset task.")
                await db.set_state(telegram_id, None)
                await message.answer("Помилка читання бази вправ; повтор скасовано.")
                return
            if task is None:
                await db.set_state(telegram_id, None)
                await message.answer("Завдання для повторення відсутнє в базі; повтор скасовано.")
                return
        elif state["mode"] == "exam":
            task = state["exam_tasks"][state["answered"]]
            task_id = task["task_id"]
            state["current_task"] = task
        else:
            try:
                used_ids = set(state.get("used_task_ids", []))
                used_records = set(state.get("used_source_records", []))
                task = None
                if state["mode"] == "practice":
                    progress = await db.open_practice_summary(telegram_id)
                    weak_topics = [
                        item for item in progress["by_topic"]
                        if item["topic"]
                        and item["cefr_level"] == state.get("level")
                        and item["attempts"] >= 2
                        and item["correct"] < item["attempts"]
                    ]
                    weak_topics.sort(
                        key=lambda item: (
                            item["correct"] / item["attempts"],
                            -item["attempts"],
                        )
                    )
                    for weak in weak_topics:
                        task = open_dataset.random_task(
                            state.get("level"),
                            used_ids,
                            topic=weak["topic"],
                            excluded_records=used_records,
                        )
                        if task:
                            break
                if task is None:
                    task = open_dataset.random_task(
                        state.get("level"), used_ids, excluded_records=used_records
                    )
            except (OSError, ValueError, sqlite3.Error):
                logger.exception("Could not select an open dataset task.")
                await db.set_state(telegram_id, None)
                await message.answer(
                    "Помилка читання навчальної бази. Перевірте файл data\\open_training.sqlite3."
                )
                return
            if task is None:
                await db.set_state(telegram_id, None)
                await message.answer(
                    "Усі неповторені завдання цього набору вичерпано. "
                    "Новий /open_practice починає окремий прохід і може повторити попередні завдання."
                )
                return
            task_id = task["task_id"]
            state.setdefault("used_task_ids", []).append(task_id)
            source_record = task.get("source_record")
            if source_record:
                state.setdefault("used_source_records", []).append(source_record)

        if task["answer_mode"] == "choice":
            correct_index = next(
                (
                    option_index
                    for option_index, option in enumerate(task["options"])
                    if normalize_lookup(option)
                    in {normalize_lookup(value) for value in task["correct_answers"]}
                ),
                None,
            )
            if correct_index is None:
                await db.set_state(telegram_id, None)
                await message.answer("Помилка в даних вправи: правильний варіант відсутній.")
                return
            display_order, correct_position = _shuffled_option_order(
                task["options"], correct_index, state.get("correct_position")
            )
            state["display_order"] = display_order
            state["correct_position"] = correct_position
        else:
            state["display_order"] = []
            state["correct_position"] = None
        state.update(
            task_id=task_id,
            attempts=0,
            hint_used=False,
            question_number=state["answered"] + 1,
        )
        await db.set_state(telegram_id, "open_practice", state)
        await show_open_task(message, telegram_id, state)

    async def submit_open_answer(
        message: Message,
        telegram_id: int,
        state: dict[str, Any],
        answer: str,
        *,
        skipped: bool = False,
    ) -> None:
        if state["mode"] == "exam":
            task = state.get("current_task")
        else:
            try:
                task = open_dataset.task(state["task_id"])
            except (OSError, ValueError, sqlite3.Error):
                logger.exception("Could not read the submitted open dataset task.")
                await db.set_state(telegram_id, None)
                await message.answer("Помилка читання навчальної бази. Поточний прохід скасовано.")
                return
        if task is None:
            await db.set_state(telegram_id, None)
            await message.answer("Поточне завдання відсутнє в стані проходження; його скасовано.")
            return
        candidate = answer
        if task["answer_mode"] == "choice" and answer.strip().isdigit():
            display_position = int(answer.strip()) - 1
            display_order = state.get(
                "display_order", list(range(len(task["options"])))
            )
            if 0 <= display_position < len(display_order):
                option_index = display_order[display_position]
                candidate = task["options"][option_index]
        correct = not skipped and normalize_lookup(candidate) in {
            normalize_lookup(value) for value in task["correct_answers"]
        }
        if state["mode"] == "exam":
            deadline = datetime.fromisoformat(state["deadline"])
            if datetime.now(UTC) >= deadline:
                await finish_open_exam(message, telegram_id, state, timed_out=True)
                return
            state.setdefault("answers", []).append(
                {
                    "task_id": task["task_id"],
                    "type": task["type"],
                    "level": task["level"],
                    "topic": task.get("topic"),
                    "correct": correct,
                    "prompt": task["prompt"],
                    "correct_answers": task["correct_answers"],
                    "explanation": task["explanation"],
                    "source_id": task["source_id"],
                    "license": task["license"],
                    "attribution": task["attribution"],
                }
            )
            state["answered"] += 1
            if state["answered"] >= state["session_limit"]:
                await finish_open_exam(message, telegram_id, state)
            else:
                await start_next_open_task(message, telegram_id, state)
            return

        if not skipped and task["answer_mode"] == "text" and not correct and state["attempts"] == 0:
            await db.record_open_practice_attempt(
                telegram_id, task["task_id"], task["type"], task["level"], False,
                state.get("hint_used", False), task.get("topic"),
            )
            state["attempts"] = 1
            state["hint_used"] = True
            await db.set_state(telegram_id, "open_practice", state)
            first = task["correct_answers"][0]
            await message.answer(
                f"Ще одна спроба. Підказка: частина мови — "
                f"{_part_of_speech_uk(task['part_of_speech'])}; "
                f"одна з відповідей починається на «{first[:1]}» і має {len(first)} символ(ів)."
            )
            return

        await db.record_open_practice_attempt(
            telegram_id, task["task_id"], task["type"], task["level"], correct,
            state.get("hint_used", False), task.get("topic"),
        )
        state["answered"] += 1
        state["correct"] = state.get("correct", 0) + int(correct)
        if state["mode"] == "mistakes":
            state["mistake_position"] += 1
        accepted = ", ".join(task["correct_answers"][:5])
        outcome = "Правильно!" if correct else "Пропуск." if skipped else "Не зовсім."
        source_note = (
            f"\nДжерело: {task['attribution']} Ліцензія: {task['license']}."
        )
        await message.answer(
            f"{outcome}\nВідповідь: {accepted}\nПояснення: {task['explanation']}"
            f"{source_note}"
        )
        await start_next_open_task(message, telegram_id, state)

    router.message.outer_middleware(restrict_to_allowed_users)
    router.callback_query.outer_middleware(restrict_to_allowed_users)

    @router.message(F.chat.type != ChatType.PRIVATE)
    async def reject_group_message(message: Message) -> None:
        await message.answer("Бот працює лише в особистому чаті.")

    @router.callback_query(F.message.chat.type != ChatType.PRIVATE)
    async def reject_group_callback(callback: CallbackQuery) -> None:
        await callback.answer("Бот працює лише в особистому чаті.", show_alert=True)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("Будь ласка, використовуйте бота в особистому чаті.")
            return
        await db.create_user(message.from_user.id)
        user = await db.get_user(message.from_user.id)
        if user and user["privacy_accepted"]:
            await _show_home(message, db)
            return
        await message.answer(
            "Вітаю! Я допоможу вивчати загальну або бізнес-англійську.\n\n"
            "Бот зберігає ваш Telegram ID, налаштування навчання та результати локально у власника бота. "
            "Telegram обробляє повідомлення як платформа. Не надсилайте чутливу особисту інформацію. "
            "Якщо ви надішлете звернення через /support, його текст і Telegram ID отримають налаштовані "
            "адміністратори підтримки. Повна чернетка політики: /privacy. "
            "Видалити дані можна командою /delete_me.\n\n"
            "Продовжуючи, ви підтверджуєте лише ознайомлення з повідомленням; правову підставу визначає власник. "
            "Базова версія розрахована на повнолітніх.",
            reply_markup=_inline([[("Прочитав(-ла), продовжити", "privacy:accept")]]),
        )

    @router.message(Command("privacy"))
    async def privacy(message: Message) -> None:
        await message.answer(
            "Політика приватності: бот використовує Telegram ID, вибраний курс, відповіді, прогрес і розклад "
            "лише для навчання та нагадувань. Дані профілю зберігаються в локальній базі власника бота; "
            "Telegram обробляє повідомлення відповідно до власних умов. Текст звернення й Telegram ID, "
            "надіслані через /support, отримують налаштовані адміністратори підтримки. Не надсилайте чутливі дані. "
            "Команда /delete_me видаляє профіль і пов'язаний прогрес.\n\n"
            "Власник/організація: [заповнити]. Контакт: [заповнити]. "
            "Мета й правова підстава обробки: [визначити власнику]. "
            "Строки: профіль після [12] місяців бездіяльності; технічні логи до [30] днів "
            "(власник має підтвердити). Резервні копії та фактичне місце зберігання: [заповнити]. "
            "Категорії користувачів: [підтвердити; базовий MVP — повнолітні]. "
            "Цей текст є чернеткою для заповнення, а не заявою про юридичну відповідність."
        )

    @router.callback_query(F.data == "privacy:accept")
    async def accept_privacy(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user:
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.update_user(callback.from_user.id, privacy_accepted=1)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Дякую. Доступна лише англійська; оберіть напрямок:",
                reply_markup=MAIN_MENU,
            )
            await _send_courses(callback.message, db)

    @router.callback_query(F.data.startswith("course:"))
    async def choose_course(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        course_id = callback.data.split(":", 1)[1]
        course = await db.get_published_course(course_id)
        if not user or not user["privacy_accepted"] or not course:
            await callback.answer("Цей курс недоступний. Спробуйте /start.", show_alert=True)
            return
        await db.update_user(
            callback.from_user.id, course_id=course_id, level=None,
            placement_score=None, placement_answers_json=None, placement_version=None, placement_at=None,
        )
        placement_data: dict[str, Any] = {
            "course_id": course_id,
            "index": 0,
            "answers": [],
        }
        first_question = course["placement_test"][0]
        display_order, correct_position = _shuffled_option_order(
            first_question["options"], first_question["correct_index"]
        )
        placement_data.update(
            display_order=display_order, correct_position=correct_position
        )
        await db.set_state(callback.from_user.id, "placement", placement_data)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Почнімо тест для курсу «{course['title']}»: 24 запитання з граматики, словника, читання й уживання мови. "
                "Результат орієнтовний, це не сертифікація."
            )
            await _show_placement_question(
                callback.message, course, 0, display_order
            )

    @router.callback_query(F.data.startswith("p:"))
    async def answer_placement(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] != "placement":
            await callback.answer("Тест уже завершений або застарів. Запустіть /start.", show_alert=True)
            return
        try:
            _, raw_index, raw_answer = callback.data.split(":")
            index, answer = int(raw_index), int(raw_answer)
            data = _state_data(user)
            if index != data["index"]:
                raise ValueError
            course = await db.get_published_course(data["course_id"])
            if not course or not 0 <= answer < len(course["placement_test"][index]["options"]):
                raise ValueError
        except (ValueError, IndexError, KeyError):
            await callback.answer("Ця відповідь застаріла. Скористайтеся поточними кнопками.", show_alert=True)
            return
        answers = [*data["answers"], answer]
        next_index = index + 1
        await callback.answer()
        if callback.message is None:
            return
        if next_index < len(course["placement_test"]):
            next_question = course["placement_test"][next_index]
            display_order, correct_position = _shuffled_option_order(
                next_question["options"],
                next_question["correct_index"],
                data.get("correct_position"),
            )
            data.update(
                index=next_index,
                answers=answers,
                display_order=display_order,
                correct_position=correct_position,
            )
            await db.set_state(callback.from_user.id, "placement", data)
            await _show_placement_question(
                callback.message, course, next_index, display_order
            )
            return
        correct, level = score_placement(course["placement_test"], answers)
        breakdown = placement_breakdown(course["placement_test"], answers)
        breakdown_text = " · ".join(
            f"{band}: {band_correct}/{total}"
            for band, (band_correct, total) in breakdown.items()
        )
        version = await db.published_course_version(course["id"])
        await db.update_user(
            callback.from_user.id,
            placement_score=correct,
            placement_answers_json=json.dumps(
                {"answers": answers, "breakdown": breakdown, "test_version": version},
                ensure_ascii=False,
            ),
            placement_version=version,
            placement_at=datetime.now(UTC).isoformat(),
            level=level,
        )
        await db.set_state(callback.from_user.id, "placement_result", {"course_id": course["id"], "suggested": level})
        await callback.message.answer(
            f"Орієнтовний рівень: {level} ({correct}/{len(answers)}). "
            f"За етапами: {breakdown_text}. "
            "Рівень визначається за послідовним проходженням рівневих етапів, а не лише сумою балів. "
            "Рівень можна змінити вручну; це не офіційна сертифікація.",
            reply_markup=_inline([
                [(f"Почати з {level}", "placement:accept")],
                [("Змінити рівень вручну", "placement:manual")],
            ]),
        )

    @router.callback_query(F.data == "placement:accept")
    async def accept_placement(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] != "placement_result":
            await callback.answer("Спочатку пройдіть тест рівня.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "pace")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Скільки хвилин на день ви готові навчатися?",
                reply_markup=_inline([[(f"{minutes} хв", f"pace:{minutes}") for minutes in (5, 10, 15, 20)]]),
            )

    @router.callback_query(F.data == "placement:manual")
    async def manual_placement(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] != "placement_result":
            await callback.answer("Спочатку пройдіть тест рівня.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "manual_level", _state_data(user))
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Оберіть бажаний стартовий рівень:",
                reply_markup=_level_keyboard(),
            )

    @router.message(Command("level"))
    async def choose_reference_level(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        arguments = (message.text or "").split(maxsplit=1)
        requested = arguments[1].strip().upper() if len(arguments) > 1 else ""
        if requested and requested not in CEFR_LEVELS:
            await message.answer("Вкажіть A1, A2, B1, B2, C1 або C2 або оберіть рівень кнопкою.")
            return
        if requested:
            await db.update_user(message.from_user.id, level=requested)
            await db.set_state(message.from_user.id, None)
            await message.answer(
                f"Рівень збережено: {requested}. {CEFR_DESCRIPTIONS[requested]}"
            )
            return
        await db.set_state(message.from_user.id, "manual_level", {"source": "command"})
        await message.answer(
            "Оберіть рівень для довідника (коротке визначення з'явиться після вибору):",
            reply_markup=_level_keyboard(),
        )

    @router.callback_query(F.data.startswith("level:"))
    async def choose_level(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        level = callback.data.split(":", 1)[1]
        if not user or user["state"] != "manual_level" or level not in CEFR_LEVELS:
            await callback.answer("Ця дія застаріла. Спробуйте /settings.", show_alert=True)
            return
        await db.update_user(callback.from_user.id, level=level)
        if _state_data(user).get("source") in {"settings", "command"}:
            await db.set_state(callback.from_user.id, None)
            await callback.answer(f"Рівень оновлено: {level}.")
            if callback.message:
                await callback.message.answer(
                    f"Стартовий рівень оновлено: {level}. {CEFR_DESCRIPTIONS[level]}",
                    reply_markup=MAIN_MENU,
                )
            return
        await db.set_state(callback.from_user.id, "pace")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Рівень {level} збережено. {CEFR_DESCRIPTIONS[level]}\nОберіть денний темп:",
                reply_markup=_inline([[(f"{minutes} хв", f"pace:{minutes}") for minutes in (5, 10, 15, 20)]]),
            )

    @router.callback_query(F.data.startswith("pace:"))
    async def choose_pace(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        try:
            minutes = int(callback.data.split(":", 1)[1])
            plan = learning_plan(minutes)
        except (ValueError, IndexError):
            await callback.answer("Оберіть 5, 10, 15 або 20 хвилин.", show_alert=True)
            return
        if not user or user["state"] not in {"pace", "settings"}:
            await callback.answer("Спочатку завершіть налаштування курсу.", show_alert=True)
            return
        await db.update_user(callback.from_user.id, minutes_per_day=minutes)
        await callback.answer()
        if user["state"] == "settings":
            if callback.message:
                await callback.message.answer(f"Темп оновлено: {minutes} хвилин на день.", reply_markup=MAIN_MENU)
            return
        await db.set_state(callback.from_user.id, "plan_confirm", {"minutes": minutes})
        if callback.message:
            await callback.message.answer(
                f"Ваш план: {plan['minutes']} хв/день, приблизно {plan['new_words']} нових слів "
                f"і {plan['exercises']} вправ за навчальний день. Підтвердити?",
                reply_markup=_inline([[("Підтвердити план", "plan:confirm")]]),
            )

    @router.callback_query(F.data == "plan:confirm")
    async def confirm_plan(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] != "plan_confirm":
            await callback.answer("План недоступний. Спробуйте /settings.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "reminder_days")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Оберіть дні нагадувань:",
                reply_markup=_inline([
                    [("Щодня", "days:daily"), ("Будні", "days:weekdays")],
                    [("Вихідні", "days:weekends"), ("Обрати окремі дні", "days:custom")],
                ]),
            )

    @router.callback_query(F.data.startswith("days:"))
    async def choose_days(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        preset = callback.data.split(":", 1)[1]
        if not user or user["state"] not in {"reminder_days", "settings_days"}:
            await callback.answer("Налаштування днів застаріли.", show_alert=True)
            return
        if preset == "custom":
            data = _state_data(user)
            data["selected_days"] = json.loads(user["reminder_days_json"])
            await db.set_state(callback.from_user.id, user["state"], data)
            await callback.answer()
            if callback.message:
                await _show_custom_days(callback.message, data["selected_days"])
            return
        if preset not in DAY_PRESETS and preset != "save":
            await callback.answer("Налаштування днів застаріли.", show_alert=True)
            return
        if preset == "save":
            data = _state_data(user)
            selected = data.get("selected_days", [])
            if not selected:
                await callback.answer("Оберіть хоча б один день.", show_alert=True)
                return
            days = sorted(set(selected))
        else:
            days = DAY_PRESETS[preset]
        await db.update_user(callback.from_user.id, reminder_days_json=json.dumps(days))
        setting = user["state"] == "settings_days"
        await db.set_state(callback.from_user.id, None if setting else "reminder_time")
        await callback.answer()
        if callback.message:
            if setting:
                await callback.message.answer("Дні нагадувань оновлено.", reply_markup=MAIN_MENU)
            else:
                await callback.message.answer(
                    "О котрій годині надсилати нагадування? Введіть 24-годинний час, наприклад 19:30."
                )

    @router.callback_query(F.data.startswith("day:"))
    async def toggle_day(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] not in {"reminder_days", "settings_days"}:
            await callback.answer("Налаштування днів застаріли.", show_alert=True)
            return
        try:
            day = int(callback.data.split(":", 1)[1])
            if not 0 <= day < 7:
                raise ValueError
        except ValueError:
            await callback.answer("Невідомий день тижня.", show_alert=True)
            return
        data = _state_data(user)
        selected = set(data.get("selected_days", json.loads(user["reminder_days_json"])))
        if day in selected:
            selected.remove(day)
        else:
            selected.add(day)
        data["selected_days"] = sorted(selected)
        await db.set_state(callback.from_user.id, user["state"], data)
        await callback.answer()
        if callback.message:
            await _show_custom_days(callback.message, data["selected_days"])

    def topics_for_level(course: dict[str, Any], level: str) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for lesson in course["lessons"]:
            if lesson["level"] != level:
                continue
            topic = re.split(r"\s+[—–-]\s+частина\b", lesson["title"], maxsplit=1, flags=re.IGNORECASE)[0]
            groups.setdefault(topic, []).append(lesson)
        return list(groups.items())

    def course_exam_pool(
        course: dict[str, Any] | None, level: str
    ) -> list[dict[str, Any]]:
        if not course:
            return []
        tasks: list[dict[str, Any]] = []
        for item in course.get("placement_test", []):
            if item.get("level") != level:
                continue
            options = item.get("options", [])
            correct_index = item.get("correct_index")
            if not isinstance(correct_index, int) or not 0 <= correct_index < len(options):
                continue
            tasks.append({
                "task_id": f"course-placement-{course['id']}-{item['id']}",
                "source_record": f"course-placement-{item['id']}",
                "type": f"placement_{item.get('skill', 'mixed')}",
                "level": level,
                "topic": item.get("skill", "placement"),
                "prompt": item["prompt"],
                "answer_mode": "choice",
                "correct_answers": [options[correct_index]],
                "options": options,
                "explanation": item.get(
                    "explanation", "У матеріалі курсу для цього питання немає окремого пояснення."
                ),
                "part_of_speech": "",
                "source_id": "bot-course",
                "license": "Навчальний матеріал цього проєкту",
                "attribution": f"Вбудований курс «{course['title']}»",
            })
        for lesson in course.get("lessons", []):
            if lesson.get("level") != level:
                continue
            for item in lesson.get("exercises", []):
                correct_answer = item.get("correct_answer")
                if not correct_answer:
                    continue
                tasks.append({
                    "task_id": f"course-exercise-{course['id']}-{item['id']}",
                    "source_record": f"course-exercise-{item['id']}",
                    "type": "course_exercise",
                    "level": level,
                    "topic": lesson["title"],
                    "prompt": item["sentence"],
                    "answer_mode": item.get("answer_mode", "choice"),
                    "correct_answers": [correct_answer],
                    "options": item.get("options", []),
                    "explanation": item.get(
                        "explanation", "У матеріалі курсу для цієї вправи немає окремого пояснення."
                    ),
                    "part_of_speech": "",
                    "source_id": "bot-course",
                    "license": "Навчальний матеріал цього проєкту",
                    "attribution": f"Вбудований курс «{course['title']}»",
                })
        unique: dict[str, dict[str, Any]] = {}
        for task in tasks:
            key = normalize_lookup(task["prompt"])
            if key:
                unique.setdefault(key, task)
        return list(unique.values())

    async def send_topics(message: Message, telegram_id: int) -> None:
        user = await _touch(db, telegram_id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        if user.get("level") not in CEFR_LEVELS:
            await message.answer("Спершу оберіть рівень командою /level.")
            return
        course = await _get_course(db, user)
        if not course:
            await _send_courses(message, db)
            return
        topics = topics_for_level(course, user["level"])
        if not topics:
            await message.answer(
                f"У курсі «{course['title']}» поки немає опублікованих уроків рівня {user['level']}. "
                "Не підміняю їх матеріалами іншого рівня. Спробуйте /open_practice або змініть рівень через /level."
            )
            return
        completed = await db.completed_lessons(telegram_id, course["id"])
        rows = []
        for title, lessons in topics:
            done = sum(lesson["id"] in completed for lesson in lessons)
            rows.append(
                [(f"{title} · уроків {len(lessons)} · завершено {done}", f"topic:{lessons[0]['id']}")]
            )
        await message.answer(
            f"Каталог тем · {user['level']} · {course['title']}\n"
            "Оберіть тему. Уроки можна проходити у довільному порядку.",
            reply_markup=_inline(rows),
        )

    @router.message(Command("topics"))
    async def show_topics(message: Message) -> None:
        await send_topics(message, message.from_user.id)

    @router.callback_query(F.data.startswith("topic:"))
    async def show_topic(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        course = await _get_course(db, user or {})
        lesson_id = callback.data.split(":", 1)[1]
        if not user or not course or user.get("level") not in CEFR_LEVELS:
            await callback.answer("Спершу оберіть курс і рівень.", show_alert=True)
            return
        anchor = next((lesson for lesson in course["lessons"] if lesson["id"] == lesson_id), None)
        if not anchor or anchor["level"] != user["level"]:
            await callback.answer("Ця тема не належить вибраному рівню.", show_alert=True)
            return
        title = re.split(
            r"\s+[—–-]\s+частина\b", anchor["title"], maxsplit=1, flags=re.IGNORECASE
        )[0]
        lessons = [
            lesson
            for lesson in course["lessons"]
            if lesson["level"] == user["level"]
            and re.split(
                r"\s+[—–-]\s+частина\b", lesson["title"], maxsplit=1, flags=re.IGNORECASE
            )[0] == title
        ]
        completed = await db.completed_lessons(callback.from_user.id, course["id"])
        rows = [
            [(
                f"{'✅ ' if lesson['id'] in completed else ''}Урок {lesson['order']}: {lesson['title']}",
                f"lesson:{lesson['id']}",
            )]
            for lesson in lessons
        ]
        rows.append([("До каталогу тем", "topics:back")])
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"{title} · {user['level']}\nУроків: {len(lessons)}. "
                "Відео показуються лише якщо їх додав модератор.",
                reply_markup=_inline(rows),
            )

    @router.callback_query(F.data == "topics:back")
    async def back_to_topics(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await send_topics(callback.message, callback.from_user.id)

    @router.callback_query(F.data.startswith("lesson:"))
    async def start_catalog_lesson(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        course = await _get_course(db, user or {})
        lesson_id = callback.data.split(":", 1)[1]
        lesson = next(
            (item for item in course["lessons"] if item["id"] == lesson_id), None
        ) if course else None
        if (
            not user
            or not course
            or not lesson
            or lesson["level"] != user.get("level")
        ):
            await callback.answer("Урок недоступний для вибраного рівня.", show_alert=True)
            return
        if user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await _send_lesson(callback.message, db, course, lesson, callback.from_user.id)

    async def start_open_block(
        message: Message, mode: str, *, level: str | None, count: int, timed: bool = False
    ) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        if user["paused"]:
            await message.answer("Навчання призупинено. Щоб продовжити, надішліть /resume.")
            return
        if not open_dataset.available:
            await message.answer(open_dataset_unavailable())
            return
        if level is not None and level not in CEFR_LEVELS:
            await message.answer("Невідомий рівень. Оберіть A1–C2 або вкажіть unknown.")
            return
        if mode != "mistakes" and level is None:
            await message.answer(
                "Рівень CEFR для цих завдань невідомий. Вони доступні лише в режимі /open_practice unknown."
            )
            return
        try:
            available_count = open_dataset.task_count(level)
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("Could not count open dataset tasks.")
            await message.answer("Помилка читання навчальної бази. Перевірте файл data\\open_training.sqlite3.")
            return
        if available_count == 0 and mode != "exam":
            level_label = level or "невідомим"
            await message.answer(
                f"У наборі немає завдань із позначкою рівня {level_label}. "
                "Рівень іншими даними не підміняю."
            )
            return
        if user.get("state") == "open_practice":
            current = _state_data(user)
            if current.get("mode") == mode:
                await message.answer("Продовжуємо збережене завдання.")
                await show_open_task(message, message.from_user.id, current)
                return
            await message.answer("Спершу завершіть або скасуйте поточний прохід командою /cancel.")
            return
        exam_tasks: list[dict[str, Any]] = []
        if mode == "exam":
            course = await _get_course(db, user)
            course_pool = course_exam_pool(course, level)
            by_type: dict[str, list[dict[str, Any]]] = {}
            for task in course_pool:
                by_type.setdefault(task["type"], []).append(task)
            course_target = min(count - 1, max(count // 2, len(by_type)))
            selected_course: list[dict[str, Any]] = []
            for task_type in sorted(by_type):
                if len(selected_course) >= course_target:
                    break
                selected_course.append(random.choice(by_type[task_type]))
            remaining_course = [
                task for task in course_pool if task not in selected_course
            ]
            random.shuffle(remaining_course)
            selected_course.extend(remaining_course[: max(0, course_target - len(selected_course))])
            exam_tasks.extend(selected_course)
            excluded_ids: set[str] = set()
            excluded_records = {
                task["source_record"] for task in selected_course if task.get("source_record")
            }
            seen_prompts = {
                normalize_lookup(task["prompt"]) for task in selected_course
            }
            while len(exam_tasks) < count and len(excluded_ids) < available_count:
                try:
                    task = open_dataset.random_task(
                        level, excluded_ids, excluded_records=excluded_records
                    )
                except (OSError, ValueError, sqlite3.Error):
                    logger.exception("Could not assemble the open dataset part of an exam.")
                    await message.answer(
                        "Помилка читання навчальної бази. Перевірте файл data\\open_training.sqlite3."
                    )
                    return
                if task is None:
                    break
                excluded_ids.add(task["task_id"])
                prompt_key = normalize_lookup(task["prompt"])
                if prompt_key in seen_prompts:
                    continue
                seen_prompts.add(prompt_key)
                if task.get("source_record"):
                    excluded_records.add(task["source_record"])
                exam_tasks.append(task)
            remaining_course = [
                task for task in course_pool if task not in selected_course
            ]
            random.shuffle(remaining_course)
            exam_tasks.extend(remaining_course[: max(0, count - len(exam_tasks))])
            random.shuffle(exam_tasks)
            if not exam_tasks:
                await message.answer(f"Для рівня {level} немає доступних завдань іспиту.")
                return
            count = min(count, len(exam_tasks))
        else:
            count = min(count, available_count)
        state: dict[str, Any] = {
            "mode": mode,
            "level": level,
            "session_limit": count,
            "answered": 0,
            "correct": 0,
            "used_task_ids": [],
        }
        if timed:
            state["deadline"] = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
            state["answers"] = []
            state["exam_tasks"] = exam_tasks
        await db.set_state(message.from_user.id, "open_practice", state)
        if timed:
            intro = (
                f"Починається тренувальний іспитовий блок: {count} завдань, 20 хвилин. "
                "Таймер перевіряється під час відповіді; після перезапуску стан і дедлайн збережуться. "
                "Це не офіційний тест НМТ/ЗНО."
            )
        else:
            level_label = level or "невідомий CEFR"
            intro = f"Починається блок із {count} завдань · {level_label}."
            if level is None:
                intro += " Усі завдання цього проходу мають невідомий рівень складності."
        await message.answer(intro)
        await start_next_open_task(message, message.from_user.id, state)

    @router.message(Command("open_practice"))
    async def open_practice(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        parts = (message.text or "").split()[1:]
        level = user.get("level")
        count_by_length = {"short": 5, "standard": 10, "long": 20}
        if parts and parts[0].casefold() == "unknown":
            level = None
            parts = parts[1:]
        elif parts and parts[0].upper() in CEFR_LEVELS:
            level = parts[0].upper()
            parts = parts[1:]
        if len(parts) > 1 or (parts and parts[0].casefold() not in count_by_length):
            await message.answer(
                "Формат: /open_practice [A1–C2|unknown] [short|standard|long]."
            )
            return
        length = parts[0].casefold() if parts else "standard"
        await start_open_block(
            message, "practice", level=level, count=count_by_length[length]
        )

    @router.message(Command("exam"))
    async def open_exam(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        if user.get("level") not in CEFR_LEVELS:
            await message.answer("Спершу оберіть рівень командою /level.")
            return
        parts = (message.text or "").split()[1:]
        if len(parts) > 1:
            await message.answer("Формат: /exam [5|10|20].")
            return
        try:
            count = int(parts[0]) if parts else 10
        except ValueError:
            await message.answer("Формат: /exam [5|10|20].")
            return
        if count not in {5, 10, 20}:
            await message.answer("Оберіть 5, 10 або 20 завдань.")
            return
        await start_open_block(
            message, "exam", level=user["level"], count=count, timed=True
        )

    @router.message(Command("mistakes"))
    async def repeat_mistakes(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start і підтвердьте повідомлення про приватність.")
            return
        if not open_dataset.available:
            await message.answer(open_dataset_unavailable())
            return
        mistakes = await db.recent_open_mistakes(message.from_user.id)
        if not mistakes:
            await message.answer(
                "У режимі відкритих завдань ще немає помилок для повторення. "
                "Вправи вбудованого курсу зберігаються окремо."
            )
            return
        if user.get("state") == "open_practice":
            await message.answer("Спершу завершіть або скасуйте поточний прохід командою /cancel.")
            return
        task_ids = [item["task_id"] for item in mistakes]
        state = {
            "mode": "mistakes",
            "level": user.get("level"),
            "session_limit": len(task_ids),
            "answered": 0,
            "correct": 0,
            "task_ids": task_ids,
            "mistake_position": 0,
        }
        await db.set_state(message.from_user.id, "open_practice", state)
        await message.answer(f"Повторюємо {len(task_ids)} нещодавніх помилок.")
        await start_next_open_task(message, message.from_user.id, state)

    @router.message(Command("vocab"))
    async def lookup_vocabulary(message: Message) -> None:
        selected = await reference_user(message)
        if not selected:
            return
        _, level = selected
        query = (message.text or "").split(maxsplit=1)
        if len(query) < 2 or not query[1].strip():
            await message.answer("Формат: /vocab англійське_слово. Показуються лише записи з рівнем CEFR вашого профілю.")
            return
        if not open_dataset.available:
            await message.answer(open_dataset_unavailable())
            return
        try:
            entries = open_dataset.lookup(query[1].strip(), level, limit=3)
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("Could not look up an open dictionary entry.")
            await message.answer("Помилка читання словника. Перевірте файл data\\open_training.sqlite3.")
            return
        if not entries:
            await message.answer(
                f"У відкритому словнику немає запису «{query[1].strip()}» рівня {level}. "
                "Невідомий або інший рівень не підставляю."
            )
            return
        for entry in entries:
            lines = [
                f"{entry['lemma']} · {_part_of_speech_uk(entry['part_of_speech'])} · {level}",
                f"Визначення англійською: {entry['definition']}",
            ]
            if entry["pronunciation"]:
                lines.append(f"Вимова: {entry['pronunciation']}")
            if entry["synonyms"]:
                lines.append("Синоніми: " + ", ".join(entry["synonyms"][:8]))
            if entry["examples"]:
                example = entry["examples"][0]
                example_text = example.get("text", "") if isinstance(example, dict) else str(example)
                if example_text:
                    lines.append(f"Приклад: {example_text}")
            lines.append(
                "Український переклад у джерелі відсутній. "
                f"Джерело: {entry['source_id']} · Open English WordNet Team, "
                "https://github.com/globalwordnet/english-wordnet. "
                "CC BY 4.0; також діють умови Princeton WordNet."
            )
            await message.answer("\n".join(lines))

    @router.message(Command("saveword"))
    async def save_vocabulary_word(message: Message) -> None:
        selected = await reference_user(message)
        if not selected:
            return
        _, level = selected
        query = (message.text or "").split(maxsplit=1)
        if len(query) < 2 or not query[1].strip():
            await message.answer("Формат: /saveword англійське_слово.")
            return
        if not open_dataset.available:
            await message.answer(open_dataset_unavailable())
            return
        try:
            entries = open_dataset.lookup(query[1].strip(), level, limit=1)
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("Could not read the open dictionary for the personal wordbook.")
            await message.answer("Помилка читання словника. Перевірте локальну навчальну базу.")
            return
        if not entries:
            await message.answer(f"Не знайдено точного CEFR-запису рівня {level}; слово не збережено.")
            return
        saved = await db.save_user_word(message.from_user.id, entries[0])
        await message.answer(
            f"{'Додано до' if saved else 'Вже є у'} вашого списку: {entries[0]['lemma']} "
            f"({_part_of_speech_uk(entries[0]['part_of_speech'])})."
        )

    @router.message(Command("wordlist"))
    async def show_wordbook(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        words = await db.user_wordbook(message.from_user.id, limit=50)
        if not words:
            await message.answer("Ваш список порожній. Додайте слово: /saveword англійське_слово.")
            return
        lines = ["Ваш особистий список слів:"]
        lines.extend(
            f"{index}. {item['word']} ({_part_of_speech_uk(item['part_of_speech'])}) · "
            f"{item['cefr_level'] or 'рівень невідомий'} — {item['definition']}"
            for index, item in enumerate(words, 1)
        )
        lines.append("Щоб видалити запис, надішліть /remove_word номер.")
        for chunk in _chunks("\n".join(lines)):
            await message.answer(chunk)

    @router.message(Command("remove_word"))
    async def remove_wordbook_word(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /remove_word номер_зі_списку.")
            return
        words = await db.user_wordbook(message.from_user.id, limit=50)
        if not words:
            await message.answer("Ваш список слів порожній.")
            return
        index = int(parts[1])
        if not 1 <= index <= len(words):
            await message.answer(f"Номер має бути від 1 до {len(words)}.")
            return
        removed = await db.remove_user_word(message.from_user.id, words[index - 1]["entry_id"])
        await message.answer("Слово видалено." if removed else "Запис уже видалено.")

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Довідка: /start — почати; /level — вибрати A1–C2; /word — слово; "
            "/grammar — граматична тема; /practice — вправа; "
            "/topics — теми й уроки; /vocab слово — словник; /saveword слово, /wordlist, /remove_word номер — особистий список; "
            "/open_practice [A1–C2|unknown] [short|standard|long] — різнорівнева практика; "
            "/exam [5|10|20] — тренувальний іспит; /mistakes — повторити помилки; "
            "/continue — відкрити урок; /cancel — скасувати поточний крок; "
            "/progress — прогрес; "
            "/profile і /plan — ваші налаштування; /settings — змінити розклад; "
            "/pause та /resume — призупинити або відновити нагадування; /support — написати підтримці; "
            "/privacy — повідомлення про дані; /delete_me — видалити профіль.\n"
            "Користуйтеся ботом в особистому чаті. Рівень після тесту орієнтовний, не сертифікаційний."
        )

    async def get_open_state(
        telegram_id: int, question_number: int
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        user = await _touch(db, telegram_id)
        if not user or user["state"] != "open_practice":
            return None
        state = _state_data(user)
        if state.get("question_number") != question_number:
            return None
        return user, state

    @router.callback_query(F.data.startswith("od:"))
    async def answer_open_choice(callback: CallbackQuery) -> None:
        try:
            _, raw_question, raw_option = callback.data.split(":")
            question_number, option_index = int(raw_question), int(raw_option)
        except (ValueError, AttributeError):
            await callback.answer("Ця відповідь некоректна.", show_alert=True)
            return
        active = await get_open_state(callback.from_user.id, question_number)
        if not active:
            await callback.answer("Це завдання застаріло.", show_alert=True)
            return
        user, state = active
        if user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        if state["mode"] == "exam":
            task = state.get("current_task")
        else:
            try:
                task = open_dataset.task(state["task_id"])
            except (OSError, ValueError, sqlite3.Error):
                logger.exception("Could not read the selected open dataset task.")
                await callback.answer("Помилка читання бази.", show_alert=True)
                return
        if not task or task["answer_mode"] != "choice" or not 0 <= option_index < len(task["options"]):
            await callback.answer("Цей варіант відповіді недоступний.", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await submit_open_answer(
                callback.message,
                callback.from_user.id,
                state,
                task["options"][option_index],
            )

    @router.callback_query(F.data.startswith("odh:"))
    async def hint_open_task(callback: CallbackQuery) -> None:
        try:
            question_number = int(callback.data.split(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("Ця підказка застаріла.", show_alert=True)
            return
        active = await get_open_state(callback.from_user.id, question_number)
        if not active:
            await callback.answer("Ця підказка застаріла.", show_alert=True)
            return
        user, state = active
        if user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        if state["mode"] == "exam":
            await callback.answer("В іспитовому режимі підказок немає.", show_alert=True)
            return
        try:
            task = open_dataset.task(state["task_id"])
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("Could not read an open dataset task for a hint.")
            await callback.answer("Помилка читання бази.", show_alert=True)
            return
        if not task:
            await callback.answer("Завдання відсутнє в базі.", show_alert=True)
            return
        state["hint_used"] = True
        await db.set_state(callback.from_user.id, "open_practice", state)
        await callback.answer()
        if callback.message:
            first = task["correct_answers"][0]
            await callback.message.answer(
                f"Підказка: частина мови — {_part_of_speech_uk(task['part_of_speech'])}; "
                f"одна з відповідей починається на «{first[:1]}» і має {len(first)} символ(ів)."
            )

    @router.callback_query(F.data.startswith("odskip:"))
    async def skip_open_task(callback: CallbackQuery) -> None:
        try:
            question_number = int(callback.data.split(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("Ця вправа застаріла.", show_alert=True)
            return
        active = await get_open_state(callback.from_user.id, question_number)
        if not active:
            await callback.answer("Ця вправа застаріла.", show_alert=True)
            return
        user, state = active
        if user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        await callback.answer("Вправу пропущено.")
        if callback.message:
            await submit_open_answer(
                callback.message, callback.from_user.id, state, "", skipped=True
            )

    @router.message(Command("word"))
    async def reference_word(message: Message) -> None:
        selected = await reference_user(message)
        if not selected:
            return
        _, level = selected
        word = reference.word(level)
        if not word:
            word_files = ["cefrj-vocabulary-profile-1.5.csv"]
            if level in {"C1", "C2"}:
                word_files.append("octanove-vocabulary-profile-c1c2-1.0.csv")
            error_file = next(
                (
                    filename
                    for filename in word_files
                    if reference.read_error(filename)
                ),
                None,
            )
            await message.answer(reference_unavailable(error_file, f"слів рівня {level}"))
            return
        project_word = reference.project_vocabulary[level].get(word["word"].casefold())
        part_of_speech = _part_of_speech_uk(word["part_of_speech"])
        lines = [
            f"Рівень {level}: {word['word']}",
            f"Частина мови: {part_of_speech}; джерело рівня: {word['source']}.",
        ]
        if project_word:
            lines.append(f"Пояснення: {project_word['translation']} (внутрішній словник бота).")
        else:
            lines.append("У профілі є слово та частина мови, але немає перекладу чи визначення.")
        example = reference.sentence(level, contains=word["word"])
        if example:
            lines.append(f"Приклад CEFR-SP: {example['text']}")
            lines.append(
                f"Джерело прикладу: {example['source_url']} · {example['license']}. "
                f"Цитування: {example['citation']}"
            )
        elif project_word and project_word.get("example"):
            lines.append(f"Приклад із навчального матеріалу бота: {project_word['example']}")
        elif reference.sentences_enabled and reference.read_error("cefr-sp.json"):
            lines.append(reference_unavailable("cefr-sp.json", f"прикладу слова рівня {level}"))
        else:
            lines.append(f"У доступних даних немає прикладу цього слова рівня {level}.")
        if not reference.sentences_enabled:
            lines.append(
                "CEFR-SP вимкнено через ліцензію CC BY-NC-SA 4.0; вмикайте лише для некомерційного використання."
            )
        lines.extend(
            [
                f"Джерело: {word['source_url']} · {word['license']}.",
                f"Цитування: {word['citation']}",
            ]
        )
        await message.answer("\n".join(lines))

    @router.message(Command("grammar"))
    async def reference_grammar(message: Message) -> None:
        selected = await reference_user(message)
        if not selected:
            return
        _, level = selected
        topic = reference.grammar_topic(level)
        if not topic:
            await message.answer(
                reference_unavailable("cefrj-grammar-profile-20180315.csv", f"граматичної теми рівня {level}")
            )
            return
        lines = [
            f"Граматична тема рівня {level}: {topic['item']}.",
            "CEFR-J позначає тему рівнем, але сам профіль не містить короткого пояснення правила.",
        ]
        example = reference.sentence(level)
        if example:
            lines.append(
                f"Приклад CEFR-SP того самого рівня (профіль не пов'язує його з темою): {example['text']}"
            )
            lines.append(
                f"Ліцензія прикладу: {example['license']}; {example['source_url']}. "
                f"Цитування: {example['citation']}"
            )
        elif not reference.sentences_enabled:
            lines.append(
                "Приклади CEFR-SP вимкнено через ліцензію CC BY-NC-SA 4.0; "
                "вмикайте лише для некомерційного використання."
            )
        else:
            lines.append(
                reference_unavailable("cefr-sp.json", f"прикладу речення рівня {level}")
            )
        lines.extend(
            [
                f"Джерело теми: {topic['source_url']} · {topic['license']}.",
                f"Цитування: {topic['citation']}",
            ]
        )
        await message.answer("\n".join(lines))

    @router.message(Command("practice"))
    async def reference_practice(message: Message) -> None:
        selected = await reference_user(message)
        if not selected:
            return
        _, level = selected
        candidate = reference.practice_word(level)
        if not candidate:
            await message.answer(
                f"У наявних даних немає вправи рівня {level}: CEFR-J не містить перекладів, "
                "а в матеріалах бота немає слова цього рівня для перевірки. "
                "Завантажте набори або спробуйте інший рівень через /level."
            )
            return
        word, project_word = candidate
        practice_data = {
            "level": level,
            "word": word["word"],
            "translation": project_word["translation"],
            "example": project_word.get("example", ""),
            "example_translation": project_word.get("example_translation", ""),
            "source": word["source"],
            "source_url": word["source_url"],
            "license": word["license"],
            "citation": word["citation"],
        }
        await db.set_state(message.from_user.id, "reference_practice", practice_data)
        await message.answer(
            f"Вправа рівня {level}: перекладіть українською слово “{word['word']}”. "
            "Надішліть свою спробу; після неї покажу відповідь. Скасувати: /cancel."
        )

    @router.message(Command("cancel"))
    async def cancel_flow(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user:
            await message.answer("Немає активного профілю. Надішліть /start.")
            return
        await db.set_state(message.from_user.id, None)
        await message.answer("Поточний крок скасовано. Ваш курс і збережений прогрес не змінено.", reply_markup=MAIN_MENU)

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        course = await _get_course(db, user)
        level = user.get("level")
        await message.answer(
            f"Напрямок: {course['title'] if course else 'ще не обрано'}\n"
            f"Рівень: {level or 'ще не визначено'}"
            f"{' — ' + CEFR_DESCRIPTIONS[level] if level in CEFR_DESCRIPTIONS else ''}\n"
            f"Темп: {user['minutes_per_day'] or 'не задано'} хв/день\n"
            f"Нагадування: {'увімкнено' if user['reminders_enabled'] else 'вимкнено'}\n"
            f"Час: {user['reminder_time']} · {user['timezone']}\n"
            f"Дні: {_format_days(json.loads(user['reminder_days_json']))}\n"
            f"Стан: {'пауза' if user['paused'] else 'активний'}",
            reply_markup=MAIN_MENU,
        )

    @router.message(Command("progress"))
    @router.message(F.text == "Мій прогрес")
    async def progress(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        course = await _get_course(db, user or {})
        if not user or not course:
            await message.answer("Оберіть напрямок через /start.")
            return
        stats = await db.progress_summary(message.from_user.id, course["id"])
        open_stats = await db.open_practice_summary(message.from_user.id)
        streak = await db.current_study_streak(message.from_user.id)
        level = user.get("level")
        level_lessons = [
            item for item in course["lessons"] if item["level"] == level
        ]
        completed = await db.completed_lessons(message.from_user.id, course["id"])
        completed_at_level = sum(item["id"] in completed for item in level_lessons)
        total_lessons = len(level_lessons)
        next_lesson_available = completed_at_level < total_lessons
        next_step = (
            "повторити слова"
            if stats["words_due"]
            else "продовжити урок"
            if next_lesson_available
            else "очікувати на нові опубліковані уроки"
        )
        weak_topics = [
            item
            for item in open_stats["by_topic"]
            if item["topic"]
            and item["attempts"] >= 2
            and item["correct"] < item["attempts"]
        ]
        weak_topics.sort(
            key=lambda item: (
                item["correct"] / item["attempts"],
                -item["attempts"],
            )
        )
        topic_summary = (
            "Теми для повторення: "
            + ", ".join(item["topic"] for item in weak_topics[:3])
            if weak_topics
            else "Тематичної статистики для адаптації поки недостатньо."
        )
        await message.answer(
            f"Ваш прогрес — {course['title']} · {level or 'рівень не визначено'}\n"
            f"Завершено уроків цього рівня: {completed_at_level}/{total_lessons}\n"
            f"Вправ: {stats['correct']}/{stats['attempts']} правильних відповідей\n"
            f"Відкриті завдання: {open_stats['correct']}/{open_stats['attempts']} правильних; "
            f"підказок використано: {open_stats['hints']}\n"
            f"{topic_summary}\n"
            f"Слів до повторення: {stats['words_due']}\n"
            f"Серія навчання: {streak} дн.\n"
            f"Наступний крок: {next_step}.\n"
            "Рівень і відсотки є орієнтовними навчальними показниками.",
            reply_markup=MAIN_MENU,
        )

    @router.message(Command("plan"))
    @router.message(F.text == "Мій план")
    async def plan_command(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        minutes = user["minutes_per_day"]
        plan = learning_plan(minutes) if minutes in (5, 10, 15, 20) else None
        await message.answer(
            f"Денна ціль: {minutes or 'не задано'} хвилин; "
            + (
                f"орієнтовно {plan['new_words']} нових слів і {plan['exercises']} вправ."
                if plan else "завершіть налаштування через /settings."
            ),
            reply_markup=MAIN_MENU,
        )

    @router.message(Command("settings"))
    @router.message(F.text == "Налаштування")
    async def settings_command(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        await db.set_state(message.from_user.id, "settings")
        await message.answer(
            "Оберіть, що змінити:",
            reply_markup=_inline([
                [("Час нагадування", "settings:time"), ("Часовий пояс", "settings:timezone")],
                [("Дні нагадувань", "settings:days"), ("Увімкнути/вимкнути нагадування", "settings:toggle")],
                [("Денний темп", "settings:pace"), ("Рівень", "settings:level")],
                [("Напрямок і тест рівня", "settings:course")],
                [("Повторити тест рівня", "settings:retest")],
            ]),
        )

    @router.callback_query(F.data == "settings:toggle")
    async def toggle_reminders(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or not user["privacy_accepted"]:
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        enabled = not bool(user["reminders_enabled"])
        await db.update_user(callback.from_user.id, reminders_enabled=enabled)
        await db.set_state(callback.from_user.id, None)
        await callback.answer("Нагадування увімкнено." if enabled else "Нагадування вимкнено.")
        if callback.message:
            await callback.message.answer(
                "Нагадування увімкнено." if enabled else "Нагадування вимкнено.",
                reply_markup=MAIN_MENU,
            )

    @router.callback_query(F.data == "settings:time")
    async def change_time(callback: CallbackQuery) -> None:
        if not await _touch(db, callback.from_user.id):
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "settings_time")
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введіть новий час у форматі HH:MM, наприклад 19:30.")

    @router.callback_query(F.data == "settings:timezone")
    async def change_timezone(callback: CallbackQuery) -> None:
        if not await _touch(db, callback.from_user.id):
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "settings_timezone")
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введіть IANA часовий пояс, наприклад Europe/Kyiv або America/Toronto.")

    @router.callback_query(F.data == "settings:days")
    async def change_days(callback: CallbackQuery) -> None:
        if not await _touch(db, callback.from_user.id):
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "settings_days")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Оберіть дні:",
                reply_markup=_inline([
                    [("Щодня", "days:daily"), ("Будні", "days:weekdays")],
                    [("Вихідні", "days:weekends"), ("Обрати окремі дні", "days:custom")],
                ]),
            )

    @router.callback_query(F.data == "settings:pace")
    async def change_pace(callback: CallbackQuery) -> None:
        if not await _touch(db, callback.from_user.id):
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "settings")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Оберіть денний темп:",
                reply_markup=_inline([[(f"{minutes} хв", f"pace:{minutes}") for minutes in (5, 10, 15, 20)]]),
            )

    @router.callback_query(F.data == "settings:level")
    async def change_level(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user:
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "manual_level", {"source": "settings"})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Оберіть стартовий рівень:",
                reply_markup=_level_keyboard(),
            )

    @router.callback_query(F.data == "settings:course")
    async def change_course(callback: CallbackQuery) -> None:
        if not await _touch(db, callback.from_user.id):
            await callback.answer("Запустіть /start.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, None)
        await callback.answer()
        if callback.message:
            await _send_courses(callback.message, db)

    @router.callback_query(F.data == "settings:retest")
    async def retake_placement(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        course = await _get_course(db, user or {})
        if not user or not course:
            await callback.answer("Спершу оберіть доступний курс через /start.", show_alert=True)
            return
        placement_data: dict[str, Any] = {
            "course_id": course["id"],
            "index": 0,
            "answers": [],
        }
        first_question = course["placement_test"][0]
        display_order, correct_position = _shuffled_option_order(
            first_question["options"], first_question["correct_index"]
        )
        placement_data.update(
            display_order=display_order, correct_position=correct_position
        )
        await db.set_state(callback.from_user.id, "placement", placement_data)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Повторне орієнтовне визначення рівня для «{course['title']}». Це не сертифікація."
            )
            await _show_placement_question(
                callback.message, course, 0, display_order
            )

    @router.message(Command("continue"))
    @router.message(F.text == "Продовжити урок")
    async def continue_lesson(message: Message) -> None:
        await _touch(db, message.from_user.id)
        await _begin_lesson(message, db, settings, message.from_user.id)

    @router.message(F.text == "Повторити слова")
    async def review_words(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        course = await _get_course(db, user or {})
        if not user or not course:
            await message.answer("Оберіть курс через /start.")
            return
        ids = await db.due_vocabulary(message.from_user.id, course["id"])
        if not ids:
            await message.answer("Зараз немає слів, запланованих до повторення.", reply_markup=MAIN_MENU)
            return
        await _begin_review(message, db, settings, course, ids, message.from_user.id)

    @router.callback_query(F.data.startswith("ex:"))
    async def answer_exercise(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if user and user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        if not user or user["state"] != "exercise":
            await callback.answer("Вправа вже завершена або застаріла.", show_alert=True)
            return
        try:
            _, raw_index, raw_option = callback.data.split(":")
            index, option_index = int(raw_index), int(raw_option)
            data = _state_data(user)
            course = await db.get_published_course(data["course_id"])
            lesson = next(item for item in course["lessons"] if item["id"] == data["lesson_id"])
            exercise = lesson["exercises"][index]
            if index != data["exercise_index"] or not 0 <= option_index < len(exercise["options"]):
                raise ValueError
        except (ValueError, IndexError, KeyError, TypeError, StopIteration):
            await callback.answer("Ця відповідь застаріла. Скористайтеся поточною вправою.", show_alert=True)
            return
        choice = exercise["options"][option_index]
        correct = choice == exercise["correct_answer"]
        attempts = data["attempts"] + 1
        await db.record_exercise_attempt(
            callback.from_user.id, course["id"], lesson["id"], exercise["id"], correct
        )
        await callback.answer("Правильно!" if correct else "Спробуйте ще раз.")
        if callback.message is None:
            return
        if not correct and attempts < exercise.get("max_attempts", 1):
            data["attempts"] = attempts
            await db.set_state(callback.from_user.id, "exercise", data)
            await callback.message.answer(
                f"Ще одна спроба. Підказка: {exercise['sentence'].replace('____', '_____')}",
                reply_markup=_exercise_keyboard(
                    exercise, index, data.get("display_order")
                ),
            )
            return
        await _explain_and_continue(
            callback.message, db, course, lesson, exercise, index, correct, callback.from_user.id
        )

    @router.callback_query(F.data.startswith("exskip:"))
    async def skip_exercise(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if user and user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        if not user or user["state"] != "exercise":
            await callback.answer("Вправа вже завершена або застаріла.", show_alert=True)
            return
        try:
            index = int(callback.data.split(":", 1)[1])
            data = _state_data(user)
            course = await db.get_published_course(data["course_id"])
            lesson = next(item for item in course["lessons"] if item["id"] == data["lesson_id"])
            exercise = lesson["exercises"][index]
            if index != data["exercise_index"]:
                raise ValueError
        except (ValueError, IndexError, KeyError, TypeError, StopIteration):
            await callback.answer("Ця вправа застаріла.", show_alert=True)
            return
        await db.record_exercise_attempt(
            callback.from_user.id, course["id"], lesson["id"], exercise["id"], False
        )
        await callback.answer("Вправу пропущено.")
        if callback.message:
            await _explain_and_continue(
                callback.message, db, course, lesson, exercise, index, False, callback.from_user.id
            )

    @router.callback_query(F.data.startswith("rv:"))
    async def answer_review(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if user and user["paused"]:
            await callback.answer("Навчання призупинено. Спершу надішліть /resume.", show_alert=True)
            return
        if not user or user["state"] != "review":
            await callback.answer("Тест повторення вже завершений або застарів.", show_alert=True)
            return
        try:
            _, raw_index, raw_choice = callback.data.split(":")
            index, choice = int(raw_index), int(raw_choice)
            data = _state_data(user)
            if index != data["review_index"] or not 0 <= choice < len(data["review_options"]):
                raise ValueError
            course = await db.get_published_course(data["course_id"])
            word_id = data["word_ids"][index]
            word = _all_words(course)[word_id]
            correct = data["review_options"][choice] == word["translation"]
        except (ValueError, IndexError, KeyError, TypeError):
            await callback.answer("Ця відповідь застаріла.", show_alert=True)
            return
        await db.record_vocabulary_answer(callback.from_user.id, course["id"], word_id, correct)
        data["correct"] += int(correct)
        if not correct:
            data.setdefault("review_wrong_ids", []).append(word_id)
        next_index = index + 1
        await callback.answer("Правильно!" if correct else f"Правильний переклад: {word['translation']}")
        if callback.message is None:
            return
        if next_index < len(data["word_ids"]):
            data["review_index"] = next_index
            await db.set_state(callback.from_user.id, "review", data)
            await _show_review_question(
                callback.message, db, course, data["word_ids"], next_index, callback.from_user.id
            )
        else:
            data["review_index"] = next_index
            await db.set_state(callback.from_user.id, "review", data)
            await _finish_review(
                callback.message, db, settings, course, callback.from_user.id, data
            )

    @router.callback_query(F.data == "review:retry")
    async def retry_review(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        if not user or user["state"] != "review_result":
            await callback.answer("Немає тесту для повторення.", show_alert=True)
            return
        data = _state_data(user)
        data.update(review_index=0, correct=0, review_wrong_ids=[])
        course = await db.get_published_course(data["course_id"])
        if not course:
            await callback.answer("Курс більше недоступний.", show_alert=True)
            return
        await db.set_state(callback.from_user.id, "review", data)
        await callback.answer()
        if callback.message:
            await _show_review_question(
                callback.message, db, course, data["word_ids"], 0, callback.from_user.id
            )

    @router.message(Command("pause"))
    async def pause(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        await db.update_user(message.from_user.id, paused=1)
        await message.answer("Навчання й нагадування призупинено. Відновити можна командою /resume.")

    @router.message(Command("resume"))
    async def resume(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Спершу запустіть /start.")
            return
        await db.update_user(message.from_user.id, paused=0)
        await message.answer("Навчання відновлено. Надішліть /continue, щоб перейти до уроку.", reply_markup=MAIN_MENU)

    @router.message(Command("delete_me"))
    async def delete_me(message: Message) -> None:
        await _touch(db, message.from_user.id)
        await message.answer(
            "Видалити профіль, налаштування, результати та прогрес без можливості відновлення?",
            reply_markup=_inline([[("Так, видалити дані", "delete:confirm"), ("Залишити дані", "delete:cancel")]]),
        )

    @router.callback_query(F.data.startswith("delete:"))
    async def confirm_delete(callback: CallbackQuery) -> None:
        if callback.data == "delete:confirm":
            deleted = await db.delete_user(callback.from_user.id)
            await callback.answer("Дані видалено." if deleted else "Профіль уже відсутній.", show_alert=True)
            if callback.message:
                await callback.message.answer("Ваш локальний профіль і пов'язаний прогрес видалено. Щоб почати знову, надішліть /start.")
        else:
            await _touch(db, callback.from_user.id)
            await callback.answer("Видалення скасовано.")
            if callback.message:
                await callback.message.answer("Профіль залишився без змін.")

    @router.message(Command("support"))
    async def support(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user or not user["privacy_accepted"]:
            await message.answer("Щоб написати підтримці, спершу запустіть /start.")
            return
        if not settings.support_admin_ids:
            await message.answer("Підтримка ще не налаштована власником бота. Зверніться до [контакту власника].")
            return
        await db.set_state(message.from_user.id, "support_message")
        await message.answer("Надішліть одним повідомленням опис питання. Не додавайте паролі чи чутливі дані.")

    @router.message(F.text, ~F.text.startswith("/"))
    async def handle_text(message: Message) -> None:
        user = await _touch(db, message.from_user.id)
        if not user:
            await message.answer("Надішліть /start, щоб створити профіль.")
            return
        if user["state"] == "open_practice":
            if user["paused"]:
                await message.answer("Навчання призупинено. Спершу надішліть /resume.")
                return
            data = _state_data(user)
            answer = message.text.strip()
            if not answer:
                await message.answer("Введіть відповідь або скористайтеся кнопкою «Пропустити».")
                return
            await submit_open_answer(message, message.from_user.id, data, answer)
            return
        if user["state"] == "reference_practice":
            data = _state_data(user)
            attempted = re.sub(r"\s+", " ", message.text).strip().casefold()
            expected = re.sub(r"\s+", " ", data.get("translation", "")).strip().casefold()
            await db.set_state(message.from_user.id, None)
            outcome = "Правильно!" if attempted == expected else "Не зовсім."
            answer = f"{outcome}\nВідповідь: {data.get('translation', 'не доступна')}."
            if data.get("example"):
                answer += f"\nПриклад: {data['example']}"
            if data.get("example_translation"):
                answer += f"\nПереклад прикладу: {data['example_translation']}"
            answer += (
                f"\nДжерело слова: {data['source']} · {data['source_url']} · {data['license']}."
                f"\nЦитування: {data['citation']}"
                "\nПереклад і приклад — із матеріалу бота."
            )
            await message.answer(answer, reply_markup=MAIN_MENU)
            return
        if user["state"] == "exercise":
            data = _state_data(user)
            try:
                course = await db.get_published_course(data["course_id"])
                if not course:
                    raise ValueError
                lesson = next(item for item in course["lessons"] if item["id"] == data["lesson_id"])
                index = data["exercise_index"]
                exercise = lesson["exercises"][index]
                if (
                    not isinstance(index, int)
                    or index != data.get("exercise_index")
                    or exercise.get("answer_mode") != "text"
                ):
                    raise ValueError
            except (ValueError, IndexError, KeyError, TypeError, StopIteration):
                await message.answer("Для цієї вправи оберіть відповідь на кнопці.")
                return
            attempted = _normalize_exercise_answer(message.text)
            if not attempted:
                await message.answer("Введіть відповідь текстом.")
                return
            correct = attempted == _normalize_exercise_answer(exercise["correct_answer"])
            attempts = data.get("attempts", 0) + 1
            await db.record_exercise_attempt(
                message.from_user.id, course["id"], lesson["id"], exercise["id"], correct
            )
            if not correct and attempts < exercise.get("max_attempts", 1):
                data["attempts"] = attempts
                await db.set_state(message.from_user.id, "exercise", data)
                await message.answer("Не зовсім. Спробуйте ще раз, ввівши слово англійською.")
                return
            await _explain_and_continue(
                message, db, course, lesson, exercise, index, correct, message.from_user.id
            )
            return
        if user["state"] == "support_message":
            if not settings.support_admin_ids:
                await db.set_state(message.from_user.id, None)
                await message.answer("Підтримка ще не налаштована.")
                return
            delivered = 0
            failures = 0
            for admin_id in settings.support_admin_ids:
                try:
                    await message.bot.send_message(
                        admin_id,
                        f"Звернення до підтримки, Telegram ID: {message.from_user.id}\n{message.text[:3000]}",
                    )
                    delivered += 1
                except TelegramAPIError as exc:
                    logger.warning("Support delivery failed (%s).", type(exc).__name__)
                    await db.record_technical_event("support_delivery_failed")
                    failures += 1
            if delivered == 0:
                await message.answer("Не вдалося доставити звернення підтримці. Спробуйте пізніше або зверніться до [контакту власника].")
                return
            await db.set_state(message.from_user.id, None)
            note = "Звернення надіслано адміністраторам підтримки."
            if failures:
                note += " Не всі налаштовані адресати отримали копію."
            await message.answer(note, reply_markup=MAIN_MENU)
            return
        if user["state"] in {"reminder_time", "settings_time"}:
            try:
                reminder_time = validate_reminder_time(message.text)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            onboarding = user["state"] == "reminder_time"
            await db.update_user(message.from_user.id, reminder_time=reminder_time)
            await db.set_state(message.from_user.id, "reminder_timezone" if onboarding else None)
            if onboarding:
                await message.answer("Введіть часовий пояс IANA (наприклад Europe/Kyiv) або надішліть Europe/Kyiv.")
            else:
                await message.answer("Час нагадування оновлено.", reply_markup=MAIN_MENU)
            return
        if user["state"] in {"reminder_timezone", "settings_timezone"}:
            try:
                timezone = validate_timezone(message.text)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            onboarding = user["state"] == "reminder_timezone"
            await db.update_user(message.from_user.id, timezone=timezone, reminders_enabled=1)
            await db.set_state(message.from_user.id, None)
            if onboarding:
                await message.answer(f"Розклад збережено: {user['reminder_time']} · {timezone}. Відкриваю перший урок.")
                await _begin_lesson(
                    message, db, settings, message.from_user.id, skip_review=True
                )
            else:
                await message.answer("Часовий пояс оновлено.", reply_markup=MAIN_MENU)
            return
        await message.answer("Не зрозуміла повідомлення. Надішліть /help або оберіть дію в меню.")

    @router.message(Command("admin_import"))
    async def admin_import(message: Message) -> None:
        if message.from_user.id not in settings.moderator_ids:
            await message.answer("Команда доступна лише модераторам.")
            return
        document = message.reply_to_message.document if message.reply_to_message else None
        if not document:
            await message.answer("Відповіддю на цю команду прикріпіть JSON-файл курсу.")
            return
        if document.file_size and document.file_size > 256_000:
            await message.answer("Файл завеликий: максимальний розмір 256 КБ.")
            return
        stream = io.BytesIO()
        try:
            await message.bot.download(document.file_id, destination=stream)
        except TelegramAPIError as exc:
            logger.warning("Moderator upload download failed (%s).", type(exc).__name__)
            await message.answer("Не вдалося завантажити файл. Перевірте з'єднання та повторіть імпорт.")
            return
        try:
            course = parse_course_json(stream.getvalue())
        except ValueError as exc:
            await message.answer(f"Імпорт не виконано: {exc}")
            return
        version = await db.add_course_version(course, "pending")
        await message.answer(
            f"Матеріал {course['id']} v{version} перевірено й збережено як чернетку. "
            f"Після перевірки опублікуйте: /admin_approve {course['id']}:{version}"
        )

    @router.message(Command("admin_pending"))
    async def admin_pending(message: Message) -> None:
        if message.from_user.id not in settings.moderator_ids:
            await message.answer("Команда доступна лише модераторам.")
            return
        pending = await db.pending_courses()
        if not pending:
            await message.answer("Чернеток на затвердження немає.")
            return
        await message.answer(
            "Очікують модерації:\n" + "\n".join(
                f"• {item['course_id']} v{item['version']}"
                for item in pending
            )
        )

    @router.message(Command("admin_approve"))
    async def admin_approve(message: Message) -> None:
        if message.from_user.id not in settings.moderator_ids:
            await message.answer("Команда доступна лише модераторам.")
            return
        match = re.fullmatch(r"/admin_approve(?:@\w+)?\s+([a-z0-9_-]{2,64}):(\d+)", message.text or "")
        if not match:
            await message.answer("Формат команди: /admin_approve course_id:версія")
            return
        approved = await db.approve_course(match.group(1), int(match.group(2)))
        await message.answer(
            "Версію курсу опубліковано." if approved else "Такої чернетки немає або її вже опубліковано."
        )

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        await message.answer("Не знаю такої команди. Перегляньте доступні команди: /help.")

    dp.include_router(router)


async def _show_placement_question(
    message: Message,
    course: dict[str, Any],
    index: int,
    display_order: list[int] | None = None,
) -> None:
    question = course["placement_test"][index]
    display_order = display_order or list(range(len(question["options"])))
    rows = [[
        (question["options"][answer], f"p:{index}:{answer}")
        for answer in display_order
    ]]
    await message.answer(
        f"Запитання {index + 1}/{len(course['placement_test'])}\n{question['prompt']}",
        reply_markup=_inline(rows),
    )


def _exercise_keyboard(
    exercise: dict[str, Any],
    index: int,
    display_order: list[int] | None = None,
) -> InlineKeyboardMarkup:
    display_order = display_order or list(range(len(exercise["options"])))
    rows = [[
        (exercise["options"][option_index], f"ex:{index}:{option_index}")
        for option_index in display_order
    ]]
    rows.append([("Пропустити вправу", f"exskip:{index}")])
    return _inline(rows)


async def _show_custom_days(message: Message, selected_days: list[int]) -> None:
    rows = [
        [
            (f"{'✓ ' if day in selected_days else ''}{DAY_NAMES[day]}", f"day:{day}")
            for day in range(start, min(start + 4, 7))
        ]
        for start in range(0, 7, 4)
    ]
    rows.append([("Зберегти дні", "days:save")])
    await message.answer("Оберіть один або кілька днів:", reply_markup=_inline(rows))


async def _explain_and_continue(
    message: Message,
    db: Database,
    course: dict[str, Any],
    lesson: dict[str, Any],
    exercise: dict[str, Any],
    index: int,
    correct: bool,
    telegram_id: int,
) -> None:
    result = "Правильно!" if correct else "Правильна відповідь:"
    await message.answer(
        f"{result} {exercise['correct_answer']}\n"
        f"Пояснення: {exercise['explanation']}"
        + (f"\nПереклад: {exercise['translation']}" if exercise.get("translation") else "")
    )
    await _begin_exercise(message, db, course, lesson, index + 1, telegram_id)


def _format_days(days: list[int]) -> str:
    return ", ".join(DAY_NAMES[day] for day in days if 0 <= day < len(DAY_NAMES)) or "не задано"


async def reminder_worker(bot: Bot, db: Database, settings: Settings) -> None:
    last_cleanup_date = ""
    while True:
        now = datetime.now(UTC)
        if now.date().isoformat() != last_cleanup_date:
            try:
                await db.cleanup_retention(
                    settings.inactive_profile_months, settings.technical_log_retention_days
                )
                last_cleanup_date = now.date().isoformat()
            except Exception:
                logger.exception("Retention cleanup failed.")
                await db.record_technical_event("retention_cleanup_failed")
        for user in await db.reminder_users():
            if not user["reminders_enabled"] or user["paused"] or not user["course_id"]:
                continue
            try:
                last_sent = await db.last_notification_date(user["telegram_id"], "study_reminder")
                due, local_date = reminder_is_due(
                    now,
                    user["timezone"],
                    user["reminder_time"],
                    json.loads(user["reminder_days_json"]),
                    last_sent,
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Invalid reminder settings encountered; reminder skipped.")
                await db.record_technical_event("invalid_reminder_settings")
                continue
            if not due or not await db.claim_notification(user["telegram_id"], local_date, "study_reminder"):
                continue
            try:
                course = await db.get_published_course(user["course_id"])
                if not course:
                    await bot.send_message(
                        user["telegram_id"],
                        "Час для англійської! Поточний курс тимчасово недоступний; зверніться до /support.",
                    )
                else:
                    completed = await db.completed_lessons(user["telegram_id"], course["id"])
                    lesson = next_lesson(course["lessons"], completed)
                    if lesson:
                        known_ids = await db.known_vocabulary(user["telegram_id"], course["id"])
                        word_map = _all_words(course)
                        known_words = [word_map[word_id] for word_id in known_ids if word_id in word_map]
                        if known_words:
                            count = learning_plan(user["minutes_per_day"] or 5)["new_words"]
                            text = "Час для англійської! Слова для повторення:\n" + "\n".join(
                                f"• {word['word']} — {word['translation']}" for word in known_words[:count]
                            )
                        else:
                            text = "Час для англійської! Відкрийте перший урок, щоб отримати слова дня."
                        text += "\n\nКоли будете готові, надішліть /continue."
                    else:
                        text = "Час для англійської! Ви завершили всі доступні уроки. Перегляньте прогрес: /progress."
                    await bot.send_message(user["telegram_id"], text, reply_markup=MAIN_MENU)
                await db.update_notification_status(user["telegram_id"], local_date, "study_reminder", "sent")
            except TelegramForbiddenError:
                await db.disable_reminders(user["telegram_id"])
                await db.update_notification_status(user["telegram_id"], local_date, "study_reminder", "blocked")
                logger.info("A reminder recipient blocked the bot; reminders were disabled.")
                await db.record_technical_event("reminder_blocked")
            except TelegramAPIError as exc:
                await db.update_notification_status(user["telegram_id"], local_date, "study_reminder", "failed")
                logger.warning("Reminder delivery failed (%s).", type(exc).__name__)
                await db.record_technical_event("reminder_delivery_failed")
        await asyncio.sleep(settings.reminder_poll_seconds)


async def configure_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Почати навчання"),
            BotCommand(command="help", description="Довідка"),
            BotCommand(command="level", description="Вибрати рівень A1–C2"),
            BotCommand(command="word", description="Слово вашого рівня"),
            BotCommand(command="grammar", description="Граматична тема вашого рівня"),
            BotCommand(command="practice", description="Вправа вашого рівня"),
            BotCommand(command="topics", description="Каталог тем і уроків"),
            BotCommand(command="vocab", description="Словник вашого рівня"),
            BotCommand(command="saveword", description="Додати слово до списку"),
            BotCommand(command="wordlist", description="Особистий список слів"),
            BotCommand(command="remove_word", description="Видалити слово зі списку"),
            BotCommand(command="open_practice", description="Відкрита практика"),
            BotCommand(command="exam", description="Тренувальний іспит"),
            BotCommand(command="mistakes", description="Повторити помилки"),
            BotCommand(command="cancel", description="Скасувати поточний крок"),
            BotCommand(command="continue", description="Продовжити урок"),
            BotCommand(command="progress", description="Мій прогрес"),
            BotCommand(command="profile", description="Мій профіль"),
            BotCommand(command="plan", description="Мій план"),
            BotCommand(command="settings", description="Налаштування"),
            BotCommand(command="pause", description="Призупинити навчання"),
            BotCommand(command="resume", description="Відновити навчання"),
            BotCommand(command="support", description="Написати підтримці"),
            BotCommand(command="privacy", description="Повідомлення про приватність"),
            BotCommand(command="delete_me", description="Видалити профіль і прогрес"),
        ]
    )
