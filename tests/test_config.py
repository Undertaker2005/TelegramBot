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


def test_dataset_settings_default_to_no_noncommercial_sentence_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEFR_SP_NONCOMMERCIAL_USE", raising=False)
    monkeypatch.setenv("CEFR_DATASET_DIR", "custom-datasets")

    settings = load_settings(require_token=False)

    assert settings.dataset_dir.as_posix() == "custom-datasets"
    assert settings.enable_cefr_sp_noncommercial is False


def test_dataset_settings_reject_invalid_license_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CEFR_SP_NONCOMMERCIAL_USE", "sometimes")

    with pytest.raises(ValueError, match="CEFR_SP_NONCOMMERCIAL_USE must be true or false"):
        load_settings(require_token=False)
