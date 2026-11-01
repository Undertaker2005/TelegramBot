from __future__ import annotations

import pytest

from telegram_bot.config import load_settings


def test_load_settings_parses_private_user_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLOWED_USER_IDS", "123, 456")

    settings = load_settings(require_token=False)

    assert settings.allowed_user_ids == frozenset({123, 456})


def test_load_settings_rejects_non_numeric_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLOWED_USER_IDS", "owner")

    with pytest.raises(ValueError, match="comma-separated integers"):
        load_settings(require_token=False)
