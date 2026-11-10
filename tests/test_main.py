import asyncio
import logging
import sys

from telegram_bot import main


def test_ctrl_c_stops_polling_without_uncaught_traceback(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(sys, "argv", ["telegram_bot.main"])
    monkeypatch.setattr(main, "load_settings", lambda **kwargs: object())
    monkeypatch.setattr(main, "_run", lambda settings: asyncio.sleep(0))

    def interrupt_polling(coroutine) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(main.asyncio, "run", interrupt_polling)

    with caplog.at_level(logging.INFO):
        main.run()

    assert "Bot stopped by user." in caplog.text
