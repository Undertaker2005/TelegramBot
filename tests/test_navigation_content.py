import json
from pathlib import Path

import pytest

from telegram_bot.tense_course import load_tense_course
from telegram_bot.video_catalog import load_video_catalog


ROOT = Path(__file__).resolve().parents[1]


def test_tense_course_has_distinct_levelled_lessons_and_answers() -> None:
    lessons = load_tense_course(ROOT / "content" / "english_tenses.json")

    assert {lesson["id"] for lesson in lessons} >= {
        "present-simple",
        "present-continuous",
        "past-simple",
        "past-continuous",
        "future-forms",
        "present-perfect",
        "present-perfect-continuous",
        "past-perfect",
    }
    assert all(
        exercise["answer"] in exercise["options"]
        for lesson in lessons
        for exercise in lesson["exercises"]
    )
    assert all(
        {"affirmative", "negative", "question"} <= lesson["forms"].keys()
        for lesson in lessons
    )


def test_youtube_catalog_has_a_verified_video_for_each_cefr_level() -> None:
    videos = load_video_catalog(ROOT / "content" / "youtube_videos.json")

    assert {video["level"] for video in videos} == {"A1", "A2", "B1", "B2", "C1", "C2"}
    assert len({video["id"] for video in videos}) == len(videos)
    assert all(video["url"].startswith("https://www.youtube.com/watch?v=") for video in videos)


def test_youtube_catalog_rejects_non_youtube_and_incomplete_entries(tmp_path: Path) -> None:
    catalog = [
        {
            "id": "sample-video",
            "title": "Sample",
            "channel": "Channel",
            "url": "https://example.com/watch?v=abc123DEF45",
            "level": "A1",
            "topic": "Home",
            "language": "Ukrainian",
            "checked_on": "2026-11-08",
        }
    ]
    path = tmp_path / "videos.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="direct HTTPS video URL"):
        load_video_catalog(path)


def test_youtube_catalog_accepts_only_complete_https_video_records(tmp_path: Path) -> None:
    catalog = [
        {
            "id": "sample-video",
            "title": "Sample",
            "channel": "Channel",
            "url": "https://www.youtube.com/watch?v=abc123DEF45",
            "level": "A1",
            "topic": "Home",
            "language": "Ukrainian",
            "checked_on": "2026-11-08",
        }
    ]
    path = tmp_path / "videos.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")

    assert load_video_catalog(path) == catalog


def test_youtube_catalog_rejects_duplicate_ids(tmp_path: Path) -> None:
    entry = {
        "id": "sample-video",
        "title": "Sample",
        "channel": "Channel",
        "url": "https://www.youtube.com/watch?v=abc123DEF45",
        "level": "A1",
        "topic": "Home",
        "language": "English",
        "checked_on": "2026-11-08",
    }
    path = tmp_path / "videos.json"
    path.write_text(json.dumps([entry, {**entry, "url": "https://www.youtube.com/watch?v=xyz123DEF45"}]), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate id"):
        load_video_catalog(path)
