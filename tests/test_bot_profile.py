from __future__ import annotations

import json
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[1]


def test_bot_profile_text_fits_telegram_limits_and_icon_is_ready_to_upload() -> None:
    profile = json.loads((ROOT / "content" / "bot_profile.json").read_text(encoding="utf-8"))
    icon_path = ROOT / "assets" / "lingopilot-icon.png"
    icon = icon_path.read_bytes()

    assert profile["name"] == "LingoPilot"
    assert "username" not in profile
    assert len(profile["name"]) <= 64
    assert len(profile["about"]) <= 120
    assert len(profile["description"]) <= 512
    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", icon[16:24]) == (512, 512)
