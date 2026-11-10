from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from telegram_bot.bot import _shuffled_option_order, configure_commands, register_handlers
from telegram_bot.config import Settings
from telegram_bot.content import validate_course
from telegram_bot.database import Database
from telegram_bot.open_dataset import OpenDataset
from telegram_bot.reference_data import ReferenceLibrary


class MemorySession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[Message] = []
        self.reply_keyboards: list[ReplyKeyboardMarkup] = []
        self.methods: list[Any] = []
        self.next_message_id = 1

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.methods.append(method)
        if not isinstance(method, SendMessage):
            return True
        if isinstance(method.reply_markup, ReplyKeyboardMarkup):
            self.reply_keyboards.append(method.reply_markup)
        message = Message(
            message_id=self.next_message_id,
            date=datetime.now(UTC),
            chat=Chat(id=int(method.chat_id), type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Learning bot"),
            text=method.text,
            reply_markup=(
                method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None
            ),
        )
        self.next_message_id += 1
        self.messages.append(message)
        return message

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""


def test_answer_options_change_order_from_the_previous_lesson_run() -> None:
    options = ["am", "are", "is", "be"]
    previous_order = [0, 1, 2, 3]

    for _ in range(20):
        order, correct_position = _shuffled_option_order(
            options, correct_index=0, previous_order=previous_order
        )
        assert order != previous_order
        assert sorted(order) == list(range(len(options)))
        assert order[correct_position] == 0
        previous_order = order


@pytest.mark.asyncio
async def test_user_completes_onboarding_and_reaches_first_exercise(tmp_path: Path) -> None:
    database = Database(tmp_path / "bot.sqlite3")
    await database.initialize()
    content_path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    await database.seed_courses(
        [validate_course(course) for course in json.loads(content_path.read_text(encoding="utf-8"))]
    )
    session = MemorySession()
    test_token = "123456:" + "A" * 35
    bot = Bot(test_token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=test_token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset({101}),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "bot.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)
    student = User(id=456, is_bot=False, first_name="Student")
    chat = Chat(id=456, type="private")
    update_id = 1

    async def send_text(text: str, target_chat: Chat = chat) -> None:
        nonlocal update_id
        incoming = Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=target_chat,
            from_user=student,
            text=text,
        )
        await dispatcher.feed_update(bot, Update(update_id=update_id, message=incoming))
        update_id += 1

    async def click(data: str) -> None:
        nonlocal update_id
        query = CallbackQuery(
            id=f"callback-{update_id}",
            from_user=student,
            chat_instance="local-test",
            message=session.messages[-1],
            data=data,
        )
        await dispatcher.feed_update(bot, Update(update_id=update_id, callback_query=query))
        update_id += 1

    try:
        await send_text("/start", Chat(id=-100123, type="group"))
        assert await database.get_user(student.id) is None
        await send_text("/start")
        assert await database.get_user(student.id)
        await click("privacy:accept")
        await click("course:english_general")
        course_user = await database.get_user(student.id)
        assert course_user["state"] == "placement", session.messages[-1].text
        placement_course = await database.get_published_course("english_general")
        await send_text("/continue")
        resumed_placement = await database.get_user(student.id)
        assert resumed_placement["state"] == "placement"
        assert json.loads(resumed_placement["state_data_json"])["index"] == 0
        assert session.messages[-1].text.startswith("Запитання 1/")
        previous_correct_position = None
        for index, question in enumerate(placement_course["placement_test"]):
            placement_state = json.loads(
                (await database.get_user(student.id))["state_data_json"]
            )
            correct_position = placement_state["correct_position"]
            assert correct_position != previous_correct_position
            previous_correct_position = correct_position
            shown_options = [
                question["options"][option_index]
                for option_index in placement_state["display_order"]
            ]
            button_labels = [
                button.text
                for row in session.messages[-1].reply_markup.inline_keyboard
                for button in row
            ]
            assert button_labels == shown_options
            incorrect_option = (question["correct_index"] + 1) % len(question["options"])
            await click(f"p:{index}:{incorrect_option}")
        assert (await database.get_user(student.id))["state"] == "placement_result"
        assert (await database.get_user(student.id))["level"] == "A1"
        await send_text("/continue")
        assert (await database.get_user(student.id))["state"] == "placement_result"
        assert session.messages[-1].text.startswith("Орієнтовний рівень: A1")
        await click("placement:accept")
        await click("pace:5")
        await click("plan:confirm")
        await click("days:daily")
        await send_text("19:30")
        await send_text("Europe/Kyiv")

        user = await database.get_user(student.id)
        assert user["privacy_accepted"] == 1
        assert user["course_id"] == "english_general"
        assert user["level"] == "A1"
        assert user["minutes_per_day"] == 5
        assert user["reminder_time"] == "19:30"
        assert user["timezone"] == "Europe/Kyiv"
        assert user["state"] is None
        assert session.reply_keyboards
        main_menu_labels = {
            button.text
            for row in session.reply_keyboards[-1].keyboard
            for button in row
        }
        assert {
            "📚 Навчання",
            "✍️ Практика",
            "📊 Прогрес",
            "👤 Профіль",
            "⚙️ Налаштування",
        } == main_menu_labels
        await send_text("📊 Прогрес")
        progress_text = session.messages[-1].text
        assert "Уроків часів завершено: 0/" in progress_text
        assert "Активних складних слів: 0" in progress_text
        await send_text("✍️ Практика")
        practice_buttons = {
            button.text
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
        }
        assert {
            "Коротка · 5",
            "Стандартна · 10",
            "Довга · 20",
            "Пробний іспит",
        } <= practice_buttons
        await send_text("📚 Навчання")
        learning_buttons = {
            button.text
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
        }
        assert {
            "📘 Новий матеріал",
            "🔁 Повторити матеріал",
            "🧠 Складні слова",
            "🕒 Англійські часи",
            "🎥 Відеоуроки",
        } <= learning_buttons
        await click("nav:new")
        topic_button = next(
            button.callback_data
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("topic:")
        )
        await click(topic_button)
        lesson_button = next(
            button.callback_data
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("lesson:")
        )
        await click(lesson_button)
        user = await database.get_user(student.id)
        assert user["state"] == "exercise"
        assert session.messages[-1].text.startswith("Вправа 1/")

        course = await database.get_published_course(user["course_id"])
        first_lesson = course["lessons"][0]
        first_exercise = first_lesson["exercises"][0]
        exercise_state = json.loads(
            (await database.get_user(student.id))["state_data_json"]
        )
        shown_options = [
            first_exercise["options"][option_index]
            for option_index in exercise_state["display_order"]
        ]
        assert [
            button.text
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("ex:")
        ] == shown_options
        wrong_index = next(
            index for index, option in enumerate(first_exercise["options"])
            if option != first_exercise["correct_answer"]
        )
        correct_index = first_exercise["options"].index(first_exercise["correct_answer"])
        await click(f"ex:0:{wrong_index}")
        retry_state = json.loads(
            (await database.get_user(student.id))["state_data_json"]
        )
        assert retry_state["attempts"] == 1
        assert retry_state["display_order"] == exercise_state["display_order"]
        await click(f"ex:0:{correct_index}")
        assert session.messages[-1].text.startswith("Вправа 2/")
        await click("exskip:1")
        assert (await database.get_user(student.id))["state"] is None
        assert (await database.progress_summary(student.id, course["id"]))["lessons"] == 1

        known_words = await database.known_vocabulary(student.id, course["id"])
        async with database._connect() as connection:
            await connection.execute(
                "UPDATE vocabulary_progress SET next_review_at = '2000-01-01T00:00:00+00:00' "
                "WHERE telegram_id = ? AND course_id = ?",
                (student.id, course["id"]),
            )
            await connection.commit()
        await send_text("/continue")
        assert (await database.get_user(student.id))["state"] == "review"
        word_map = {
            word["id"]: word
            for lesson in course["lessons"]
            for word in lesson["vocabulary"]
        }

        async def answer_review(*, correct: bool) -> None:
            state = json.loads((await database.get_user(student.id))["state_data_json"])
            word_id = state["word_ids"][state["review_index"]]
            translation = word_map[word_id]["translation"]
            correct_choice = state["review_options"].index(translation)
            choice = correct_choice if correct else (correct_choice + 1) % len(state["review_options"])
            await click(f"rv:{state['review_index']}:{choice}")
            updated = await database.get_user(student.id)
            if updated["state"] in {"review", "review_result"}:
                updated_data = json.loads(updated["state_data_json"])
                assert updated_data["correct"] == state["correct"] + int(correct), (
                    word_id, translation, state["review_options"], choice, updated_data,
                    session.messages[-1].text,
                )

        for _ in known_words:
            await answer_review(correct=False)
        assert (await database.get_user(student.id))["state"] == "review_result"
        await send_text("/continue")
        assert "Спершу завершіть повторний тест" in session.messages[-1].text
        await click("review:retry")
        for _ in known_words:
            await answer_review(correct=True)

        resumed = await database.get_user(student.id)
        assert resumed["state"] == "exercise", (resumed["state_data_json"], session.messages[-1].text)
        lesson_state = json.loads(resumed["state_data_json"])
        assert lesson_state["lesson_id"] == course["lessons"][1]["id"]
        await send_text("/pause")
        await click("ex:0:0")
        assert (await database.get_user(student.id))["state"] == "exercise"
        await send_text("/continue")
        assert "Навчання призупинено" in session.messages[-1].text
        await send_text("/resume")
        await send_text("/cancel")
        await send_text("/settings")
        await click("settings:retest")
        assert (await database.get_user(student.id))["state"] == "placement"
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_exam_withholds_answers_until_completion_and_persists_results(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "exam.sqlite3")
    await database.initialize()
    content_path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    courses = [
        validate_course(course)
        for course in json.loads(content_path.read_text(encoding="utf-8"))
    ]
    await database.seed_courses(courses)
    dataset_path = tmp_path / "open_training.sqlite3"
    with sqlite3.connect(dataset_path) as connection:
        connection.execute(
            "CREATE TABLE dictionary_entries (entry_id TEXT, lemma TEXT, lemma_norm TEXT, "
            "part_of_speech TEXT, pronunciation TEXT, definition TEXT, examples_json TEXT, "
            "sense_id TEXT, synset_id TEXT, synonyms_json TEXT, cefr_level TEXT, "
            "level_source TEXT, lexical_field TEXT, source_id TEXT)"
        )
        connection.execute(
            "INSERT INTO dictionary_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "apple-a1", "apple", "apple", "noun", None, "A fruit.", "[]",
                "apple-sense", "apple-synset", "[]", "A1", "CEFR-J exact match",
                "Food", "oewn-2025",
            ),
        )
        connection.execute(
            "CREATE TABLE learning_tasks (task_id TEXT, task_type TEXT, instruction TEXT, "
            "cefr_level TEXT, level_source TEXT, topic TEXT, lesson TEXT, prompt TEXT, "
            "answer_mode TEXT, correct_answers_json TEXT, options_json TEXT, explanation TEXT, "
            "source_id TEXT, source_record TEXT, license TEXT, attribution TEXT, "
            "created_by TEXT, generation_method TEXT, part_of_speech TEXT)"
        )
        for task_id, prompt, answer in (
            ("open-apple", "A fruit that grows on a tree.", "apple"),
            ("open-book", "A set of printed pages.", "book"),
        ):
            connection.execute(
                "INSERT INTO learning_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, "definition_to_word", "Level A1", "A1", "CEFR-J",
                    "Vocabulary", None, prompt, "text", json.dumps([answer]), "[]",
                    "Open dataset explanation.", "oewn-2025", task_id, "CC BY 4.0",
                    "Open English WordNet", "project", "test fixture", "noun",
                ),
            )
    open_dataset = OpenDataset(dataset_path)

    session = MemorySession()
    token = "123456:" + "C" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "exam.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings, open_dataset=open_dataset)
    student = User(id=456, is_bot=False, first_name="Student")
    chat = Chat(id=456, type="private")
    update_id = 1

    async def send_text(text: str) -> None:
        nonlocal update_id
        message = Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=student,
            text=text,
        )
        await dispatcher.feed_update(bot, Update(update_id=update_id, message=message))
        update_id += 1

    try:
        await database.create_user(student.id)
        await database.update_user(
            student.id,
            privacy_accepted=1,
            course_id="english_general",
            level="A1",
        )
        await send_text("/vocab apple")
        assert "apple · іменник · A1" in session.messages[-1].text
        assert "Український переклад у джерелі відсутній." in session.messages[-1].text
        await send_text("/saveword apple")
        await send_text("/wordlist")
        assert "apple (іменник)" in session.messages[-1].text
        await send_text("/remove_word 1")
        assert await database.user_wordbook(student.id) == []

        await send_text("/exam 5")
        state = json.loads((await database.get_user(student.id))["state_data_json"])
        assert state["mode"] == "exam"
        assert state["session_limit"] == 5
        assert any(task["type"] == "placement_reading" for task in state["exam_tasks"])
        assert {task["source_id"] for task in state["exam_tasks"]} == {
            "bot-course",
            "oewn-2025",
        }

        for index in range(5):
            user = await database.get_user(student.id)
            state = json.loads(user["state_data_json"])
            task = state["current_task"]
            answer = "not-a-valid-answer" if index == 0 else task["correct_answers"][0]
            await send_text(answer)
            if index < 4:
                assert "Відповідь:" not in session.messages[-1].text
                assert "Пояснення:" not in session.messages[-1].text

        assert (await database.get_user(student.id))["state"] is None
        assert "Розбір помилок:" in session.messages[-1].text
        summary = await database.open_practice_summary(student.id)
        assert summary["attempts"] == 5
        assert summary["correct"] == 4
        assert len(await database.recent_open_mistakes(student.id)) == 1
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_navigation_tenses_difficult_words_and_levelled_video_catalog(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "navigation.sqlite3")
    await database.initialize()
    content_path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    courses = [
        validate_course(course)
        for course in json.loads(content_path.read_text(encoding="utf-8"))
    ]
    await database.seed_courses(courses)
    await database.create_user(789)
    await database.update_user(
        789,
        privacy_accepted=1,
        course_id="english_general",
        level="A1",
    )
    await database.record_learning_mistake(
        789,
        item="apple",
        attempted_answer="pear",
        correct_answer="apple",
        accepted_answers=["apple"],
        translation="яблуко",
        example="I ate an apple.",
        topic="Їжа",
        level="A1",
        explanation="Apple означає «яблуко».",
    )

    session = MemorySession()
    token = "123456:" + "D" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "navigation.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)
    student = User(id=789, is_bot=False, first_name="Student")
    chat = Chat(id=789, type="private")
    update_id = 1

    async def send_text(text: str) -> None:
        nonlocal update_id
        message = Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=student,
            text=text,
        )
        await dispatcher.feed_update(bot, Update(update_id=update_id, message=message))
        update_id += 1

    async def click(data: str) -> None:
        nonlocal update_id
        query = CallbackQuery(
            id=f"callback-{update_id}",
            from_user=student,
            chat_instance="local-test",
            message=session.messages[-1],
            data=data,
        )
        await dispatcher.feed_update(bot, Update(update_id=update_id, callback_query=query))
        update_id += 1

    try:
        await send_text("🕒 Вивчати часи")
        assert "Курс англійських часів" in session.messages[-1].text
        assert (await database.get_user(789))["state"] is None
        await click("tense:lesson:present-simple")
        assert "Ствердження:" in session.messages[-1].text
        assert "She works in a school." in session.messages[-1].text
        assert (await database.get_user(789))["state"] is None

        await click("tense:start:present-simple")
        user = await database.get_user(789)
        state = json.loads(user["state_data_json"])
        assert user["state"] == "tense_practice"
        first_question = state["question_index"]
        correct_option = next(
            index
            for index, option in enumerate(
                json.loads(
                    (Path(__file__).resolve().parents[1] / "content" / "english_tenses.json")
                    .read_text(encoding="utf-8")
                )[0]["exercises"][0]["options"]
            )
            if option
            == json.loads(
                (Path(__file__).resolve().parents[1] / "content" / "english_tenses.json")
                .read_text(encoding="utf-8")
            )[0]["exercises"][0]["answer"]
        )
        await click(f"tense:answer:present-simple:{first_question}:{correct_option}")
        state = json.loads((await database.get_user(789))["state_data_json"])
        assert state["question_index"] == 1
        await click(f"tense:answer:present-simple:{first_question}:{correct_option}")
        tense_attempts = await database.tense_progress(789)
        assert tense_attempts[0]["attempts"] == 1

        second_exercise = json.loads(
            (Path(__file__).resolve().parents[1] / "content" / "english_tenses.json")
            .read_text(encoding="utf-8")
        )[0]["exercises"][1]
        await send_text(second_exercise["answer"])
        assert (await database.get_user(789))["state"] is None
        tense_attempts = await database.tense_progress(789)
        assert tense_attempts[0]["attempts"] == 2
        assert tense_attempts[0]["correct"] == 2
        assert tense_attempts[0]["completed_at"]

        await send_text("🧠 Слова, які я плутаю")
        assert "apple — яблуко" in session.messages[-1].text
        await click("words:start")
        assert "Перекладіть українською: apple" in session.messages[-1].text
        await send_text("яблуко")
        assert (await database.get_user(789))["state"] is None
        history = await database.learning_mistakes(789, active_only=False)
        assert history[0]["correct_streak"] == 1

        await send_text("🎥 Відеоуроки")
        video_button = next(
            button.callback_data
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("video:")
        )
        assert video_button == "video:a1-daily-routine"
        await click(video_button)
        assert "A1 English Listening Practice - Daily Routine" in session.messages[-1].text
        assert any(
            button.url == "https://www.youtube.com/watch?v=aQ0w2I0Eb9I"
            for row in session.messages[-1].reply_markup.inline_keyboard
            for button in row
        )
        await send_text("/where")
        assert "Де я зараз:" in session.messages[-1].text
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_moderator_commands_are_restricted_to_allowlisted_ids(tmp_path: Path) -> None:
    database = Database(tmp_path / "moderation.sqlite3")
    await database.initialize()
    session = MemorySession()
    token = "123456:" + "B" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset({101}),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "moderation.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)

    async def command(user_id: int) -> None:
        message = Message(
            message_id=user_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Moderator test"),
            text="/admin_pending",
        )
        await dispatcher.feed_update(bot, Update(update_id=user_id, message=message))

    try:
        await command(456)
        assert session.messages[-1].text == "Команда доступна лише модераторам."
        await command(101)
        assert session.messages[-1].text == "Чернеток на затвердження немає."
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_private_allowlist_blocks_non_owner_from_creating_a_profile(tmp_path: Path) -> None:
    database = Database(tmp_path / "private.sqlite3")
    await database.initialize()
    session = MemorySession()
    token = "123456:" + "C" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset({123}),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "private.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=456, type="private"),
        from_user=User(id=456, is_bot=False, first_name="Guest"),
        text="/start",
    )

    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
        assert await database.get_user(456) is None
        assert session.messages[-1].text == "Цей бот доступний лише власнику."
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_group_and_channel_updates_are_rejected_without_creating_profiles(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "private-chats.sqlite3")
    await database.initialize()
    session = MemorySession()
    token = "123456:" + "E" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "private-chats.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)
    student = User(id=456, is_bot=False, first_name="Student")
    group = Chat(id=-100123, type="supergroup")

    try:
        group_command = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=group,
            from_user=student,
            text="/progress",
        )
        await dispatcher.feed_update(bot, Update(update_id=1, message=group_command))
        assert await database.get_user(student.id) is None
        assert session.messages[-1].text == "LingoPilot працює лише в особистому чаті."

        group_callback_message = Message(
            message_id=2,
            date=datetime.now(UTC),
            chat=group,
            from_user=User(id=bot.id, is_bot=True, first_name="LingoPilot"),
            text="Навчання",
        )
        group_callback = CallbackQuery(
            id="group-callback",
            from_user=student,
            chat_instance="group",
            message=group_callback_message,
            data="nav:home",
        )
        await dispatcher.feed_update(
            bot, Update(update_id=2, callback_query=group_callback)
        )
        assert await database.get_user(student.id) is None

        channel = Chat(id=-100456, type="channel", title="Test channel")
        channel_post = Message(
            message_id=3,
            date=datetime.now(UTC),
            chat=channel,
            text="/start",
        )
        await dispatcher.feed_update(
            bot, Update(update_id=3, channel_post=channel_post)
        )
        assert await database.get_user(student.id) is None
        assert session.messages[-1].text == "LingoPilot працює лише в особистому чаті."
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_bot_commands_are_only_configured_for_private_chats() -> None:
    token = "123456:" + "F" * 35
    session = MemorySession()
    bot = Bot(token, session=session)
    try:
        await configure_commands(bot)
        deleted_scopes = [
            method.scope.type.value if method.scope else None
            for method in session.methods
            if method.__api_method__ == "deleteMyCommands"
        ]
        set_commands = next(
            method
            for method in session.methods
            if method.__api_method__ == "setMyCommands"
        )
        assert deleted_scopes == [None, "all_group_chats"]
        assert set_commands.scope.type.value == "all_private_chats"
        assert any(command.command == "progress" for command in set_commands.commands)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_reference_commands_use_saved_level_and_reveal_practice_answer_after_attempt(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "reference.sqlite3")
    await database.initialize()
    await database.create_user(789)
    await database.update_user(789, privacy_accepted=1, level="A1")
    session = MemorySession()
    token = "123456:" + "D" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "reference.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    reference = ReferenceLibrary(
        vocabulary={
            "A1": [{
                "word": "apple",
                "part_of_speech": "noun",
                "source": "CEFR-J",
                "source_url": "source",
                "license": "license",
                "citation": "citation",
            }],
            "A2": [{
                "word": "journey",
                "part_of_speech": "noun",
                "source": "CEFR-J",
                "source_url": "source",
                "license": "license",
                "citation": "citation",
            }],
            "B1": [],
            "B2": [],
            "C1": [],
            "C2": [{
                "word": "abstruse",
                "part_of_speech": "adjective",
                "source": "CEFR-J",
                "source_url": "source",
                "license": "license",
                "citation": "citation",
            }],
        },
        grammar={
            "A1": [{"item": "Present simple", "source_url": "source", "license": "license", "citation": "citation"}],
            "A2": [{"item": "Past simple", "source_url": "source", "license": "license", "citation": "citation"}],
            "B1": [],
            "B2": [],
            "C1": [],
            "C2": [],
        },
        sentences={
            "A1": [{
                "text": "I have an apple.",
                "license": "CC BY-NC-SA 4.0",
                "source_url": "source",
                "citation": "citation",
            }],
            "A2": [{
                "text": "The journey was long.",
                "license": "CC BY-NC-SA 4.0",
                "source_url": "source",
                "citation": "citation",
            }],
            "B1": [],
            "B2": [],
            "C1": [],
            "C2": [{
                "text": "The abstruse text perplexed her.",
                "license": "CC BY-NC-SA 4.0",
                "source_url": "source",
                "citation": "citation",
            }],
        },
        project_vocabulary={
            "A1": {
                "apple": {
                    "translation": "яблуко",
                    "example": "I have an apple.",
                    "example_translation": "У мене є яблуко.",
                }
            },
            "A2": {},
            "B1": {},
            "B2": {},
            "C1": {},
            "C2": {},
        },
        errors={},
        sentences_enabled=True,
    )
    register_handlers(dispatcher, database, settings, reference)

    async def send(text: str, message_id: int) -> str:
        incoming = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=789, type="private"),
            from_user=User(id=789, is_bot=False, first_name="Student"),
            text=text,
        )
        await dispatcher.feed_update(bot, Update(update_id=message_id, message=incoming))
        return session.messages[-1].text

    try:
        word_response = await send("/word", 1)
        assert "apple" in word_response
        assert "journey" not in word_response and "abstruse" not in word_response
        assert "https://" not in word_response and "github.com" not in word_response
        grammar_response = await send("/grammar", 2)
        assert "Present simple" in grammar_response
        assert "The journey was long." not in grammar_response
        assert "https://" not in grammar_response and "github.com" not in grammar_response

        assert "перекладіть" in await send("/practice", 3)
        practice_response = await send("яблуко", 4)
        assert "Правильно!" in practice_response
        assert "Відповідь: яблуко." in practice_response
        assert (await database.get_user(789))["state"] is None

        level_response = await send("/level C2", 5)
        assert level_response.startswith("Рівень збережено: C2.")
        assert "Майже вільне володіння" in level_response
        assert (await database.get_user(789))["level"] == "C2"
        c2_response = await send("/word", 6)
        assert "abstruse" in c2_response
        assert "I have an apple." not in c2_response
        assert "Не знаю такої команди" in await send("/not_a_command", 7)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_text_answer_exercise_accepts_retry_and_records_both_attempts(tmp_path: Path) -> None:
    database = Database(tmp_path / "text-exercise.sqlite3")
    await database.initialize()
    content_path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    courses = [
        validate_course(course)
        for course in json.loads(content_path.read_text(encoding="utf-8"))
    ]
    await database.seed_courses(courses)
    course = next(course for course in courses if course["track"] == "general")
    lesson = next(
        lesson for lesson in course["lessons"] if lesson["id"] == "catalog-general-1-1"
    )
    exercise_index = next(
        index for index, item in enumerate(lesson["exercises"])
        if item.get("answer_mode") == "text"
    )
    exercise = lesson["exercises"][exercise_index]
    await database.create_user(789)
    await database.update_user(789, privacy_accepted=1, course_id=course["id"])
    await database.set_state(
        789,
        "exercise",
        {
            "course_id": course["id"],
            "lesson_id": lesson["id"],
            "exercise_index": exercise_index,
            "attempts": 0,
        },
    )

    session = MemorySession()
    token = "123456:" + "E" * 35
    bot = Bot(token, session=session)
    dispatcher = Dispatcher()
    settings = Settings(
        bot_token=token,
        allowed_user_ids=frozenset(),
        moderator_ids=frozenset(),
        support_admin_ids=frozenset(),
        database_path=tmp_path / "text-exercise.sqlite3",
        log_path=tmp_path / "bot.log",
        inactive_profile_months=12,
        technical_log_retention_days=30,
        vocabulary_pass_threshold=0.7,
        reminder_poll_seconds=30,
    )
    register_handlers(dispatcher, database, settings)

    async def send(text: str, message_id: int) -> str:
        incoming = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=789, type="private"),
            from_user=User(id=789, is_bot=False, first_name="Student"),
            text=text,
        )
        await dispatcher.feed_update(bot, Update(update_id=message_id, message=incoming))
        return session.messages[-1].text

    try:
        retry = await send("not the answer", 1)
        assert "Спробуйте ще раз" in retry
        state = await database.get_user(789)
        assert state["state"] == "exercise"
        assert json.loads(state["state_data_json"])["attempts"] == 1

        result = await send(f"  {exercise['correct_answer'].upper()}  ", 2)
        assert "Правильно!" in session.messages[-2].text
        assert exercise["correct_answer"] in session.messages[-2].text
        assert result.startswith("Вправа 9/10:")
        state = await database.get_user(789)
        assert state["state"] == "exercise"
        assert json.loads(state["state_data_json"])["exercise_index"] == 8
        async with database._connect() as connection:
            cursor = await connection.execute(
                "SELECT was_correct FROM exercise_attempts WHERE exercise_id = ? ORDER BY id",
                (exercise["id"],),
            )
            attempts = await cursor.fetchall()
        assert [row["was_correct"] for row in attempts] == [0, 1]
    finally:
        await bot.session.close()
