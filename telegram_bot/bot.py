from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
from datetime import UTC, datetime
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
    reminder_is_due,
    review_passed,
    score_placement,
    study_streak,
    validate_reminder_time,
    validate_timezone,
)

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
    data = {
        "course_id": course["id"],
        "lesson_id": lesson["id"],
        "exercise_index": exercise_index,
        "attempts": attempts,
    }
    await db.set_state(telegram_id, "exercise", data)
    rows = [[(option, f"ex:{exercise_index}:{index}") for index, option in enumerate(exercise["options"])]]
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
    lesson = next_lesson(course["lessons"], completed)
    if not lesson:
        await message.answer("Ви завершили всі доступні уроки цього курсу. Нові матеріали з'являться після модерації.")
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


def register_handlers(dp: Dispatcher, db: Database, settings: Settings) -> None:
    router = Router()

    async def restrict_to_allowed_users(handler: Any, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        if settings.allowed_user_ids and event.from_user.id not in settings.allowed_user_ids:
            if isinstance(event, Message):
                await event.answer("Цей бот доступний лише власнику.")
            else:
                await event.answer("Цей бот доступний лише власнику.", show_alert=True)
            return None
        return await handler(event, data)

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
        await db.set_state(callback.from_user.id, "placement", {"course_id": course_id, "index": 0, "answers": []})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Почнімо короткий тест для курсу «{course['title']}». Це лише орієнтовне визначення рівня, не сертифікація."
            )
            await _show_placement_question(callback.message, course, 0)

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
            data.update(index=next_index, answers=answers)
            await db.set_state(callback.from_user.id, "placement", data)
            await _show_placement_question(callback.message, course, next_index)
            return
        correct, level = score_placement(course["placement_test"], answers)
        version = await db.published_course_version(course["id"])
        await db.update_user(
            callback.from_user.id,
            placement_score=correct,
            placement_answers_json=json.dumps({"answers": answers, "test_version": version}, ensure_ascii=False),
            placement_version=version,
            placement_at=datetime.now(UTC).isoformat(),
            level=level,
        )
        await db.set_state(callback.from_user.id, "placement_result", {"course_id": course["id"], "suggested": level})
        await callback.message.answer(
            f"Ваш орієнтовний результат: {level} ({correct}/{len(answers)} правильних відповідей). "
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
                reply_markup=_inline([[(level, f"level:{level}") for level in ("A1", "A2", "B1", "B2", "C1")]]),
            )

    @router.callback_query(F.data.startswith("level:"))
    async def choose_level(callback: CallbackQuery) -> None:
        user = await _touch(db, callback.from_user.id)
        level = callback.data.split(":", 1)[1]
        if not user or user["state"] != "manual_level" or level not in {"A1", "A2", "B1", "B2", "C1"}:
            await callback.answer("Ця дія застаріла. Спробуйте /settings.", show_alert=True)
            return
        await db.update_user(callback.from_user.id, level=level)
        if _state_data(user).get("source") == "settings":
            await db.set_state(callback.from_user.id, None)
            await callback.answer(f"Рівень оновлено: {level}.")
            if callback.message:
                await callback.message.answer(f"Стартовий рівень оновлено: {level}.", reply_markup=MAIN_MENU)
            return
        await db.set_state(callback.from_user.id, "pace")
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Рівень {level} збережено. Оберіть денний темп:",
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

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Довідка: /start — почати; /continue — відкрити урок; /cancel — скасувати поточний крок; "
            "/progress — прогрес; "
            "/profile і /plan — ваші налаштування; /settings — змінити розклад; "
            "/pause та /resume — призупинити або відновити нагадування; /support — написати підтримці; "
            "/privacy — повідомлення про дані; /delete_me — видалити профіль.\n"
            "Користуйтеся ботом в особистому чаті. Рівень після тесту орієнтовний, не сертифікаційний."
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
        await message.answer(
            f"Напрямок: {course['title'] if course else 'ще не обрано'}\n"
            f"Рівень: {user['level'] or 'ще не визначено'}\n"
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
        streak = await db.current_study_streak(message.from_user.id)
        total_lessons = len(course["lessons"])
        next_lesson_available = stats["lessons"] < total_lessons
        next_step = (
            "повторити слова"
            if stats["words_due"]
            else "продовжити урок"
            if next_lesson_available
            else "очікувати на нові опубліковані уроки"
        )
        await message.answer(
            f"Ваш прогрес — {course['title']} · {user['level'] or 'рівень не визначено'}\n"
            f"Завершено уроків: {stats['lessons']}/{total_lessons}\n"
            f"Вправ: {stats['correct']}/{stats['attempts']} правильних відповідей\n"
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
                reply_markup=_inline([[(level, f"level:{level}") for level in ("A1", "A2", "B1", "B2", "C1")]]),
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
        await db.set_state(
            callback.from_user.id,
            "placement",
            {"course_id": course["id"], "index": 0, "answers": []},
        )
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Повторне орієнтовне визначення рівня для «{course['title']}». Це не сертифікація."
            )
            await _show_placement_question(callback.message, course, 0)

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
                reply_markup=_exercise_keyboard(exercise, index),
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

    dp.include_router(router)


async def _show_placement_question(message: Message, course: dict[str, Any], index: int) -> None:
    question = course["placement_test"][index]
    rows = [[(option, f"p:{index}:{answer}") for answer, option in enumerate(question["options"])]]
    await message.answer(
        f"Запитання {index + 1}/{len(course['placement_test'])}\n{question['prompt']}",
        reply_markup=_inline(rows),
    )


def _exercise_keyboard(exercise: dict[str, Any], index: int) -> InlineKeyboardMarkup:
    rows = [[(option, f"ex:{index}:{option_index}") for option_index, option in enumerate(exercise["options"])]]
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
