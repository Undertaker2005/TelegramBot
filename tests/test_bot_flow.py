from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from telegram_bot.bot import register_handlers
from telegram_bot.config import Settings
from telegram_bot.content import validate_course
from telegram_bot.database import Database


class MemorySession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[Message] = []
        self.next_message_id = 1

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        if not isinstance(method, SendMessage):
            return True
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
        for index, option in enumerate((1, 0, 0, 0, 0)):
            await click(f"p:{index}:{option}")
        assert (await database.get_user(student.id))["state"] == "placement_result"
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
        assert user["state"] == "exercise"
        assert session.messages[-1].text.startswith("Вправа 1/")

        course = await database.get_published_course(user["course_id"])
        first_lesson = course["lessons"][0]
        first_exercise = first_lesson["exercises"][0]
        wrong_index = next(
            index for index, option in enumerate(first_exercise["options"])
            if option != first_exercise["correct_answer"]
        )
        correct_index = first_exercise["options"].index(first_exercise["correct_answer"])
        await click(f"ex:0:{wrong_index}")
        assert json.loads((await database.get_user(student.id))["state_data_json"])["attempts"] == 1
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
        await send_text("/settings")
        await click("settings:retest")
        assert (await database.get_user(student.id))["state"] == "placement"
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
