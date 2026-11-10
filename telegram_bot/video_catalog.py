from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from telegram_bot.reference_data import CEFR_LEVELS


_VIDEO_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def load_video_catalog(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the YouTube catalog at {path}.") from exc
    if not isinstance(raw, list):
        raise ValueError("The YouTube catalog must be a JSON list.")

    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"YouTube catalog entry {index + 1} must be an object.")
        required = ("id", "title", "channel", "url", "level", "topic", "language", "checked_on")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
            raise ValueError(f"YouTube catalog entry {index + 1} is missing required text fields.")
        if not _VIDEO_ID.fullmatch(item["id"]):
            raise ValueError(f"YouTube catalog entry {index + 1} has an invalid id.")
        if item["id"] in seen_ids:
            raise ValueError(f"YouTube catalog contains a duplicate id: {item['id']}")
        if item["level"] not in CEFR_LEVELS:
            raise ValueError(f"YouTube catalog entry {index + 1} has an invalid CEFR level.")
        parsed = urlparse(item["url"])
        valid_youtube_url = (
            parsed.scheme == "https"
            and parsed.hostname in {"www.youtube.com", "youtube.com"}
            and parsed.path == "/watch"
            and bool(parse_qs(parsed.query).get("v"))
        ) or (
            parsed.scheme == "https"
            and parsed.hostname == "youtu.be"
            and bool(parsed.path.strip("/"))
        )
        if not valid_youtube_url:
            raise ValueError(f"YouTube catalog entry {index + 1} is not a direct HTTPS video URL.")
        try:
            date.fromisoformat(item["checked_on"])
        except ValueError as exc:
            raise ValueError(f"YouTube catalog entry {index + 1} has an invalid checked_on date.") from exc
        url = item["url"].strip()
        if url in seen:
            raise ValueError(f"YouTube catalog contains a duplicate URL: {url}")
        seen_ids.add(item["id"])
        seen.add(url)
        videos.append({**item, "url": url})
    return videos
