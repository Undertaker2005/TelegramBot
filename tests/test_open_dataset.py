import json
import sqlite3
from pathlib import Path

import pytest

from telegram_bot.open_dataset import OpenDataset


@pytest.fixture
def open_dataset(tmp_path: Path) -> OpenDataset:
    path = tmp_path / "open_training.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE dictionary_entries (
                entry_id TEXT, lemma TEXT, lemma_norm TEXT, part_of_speech TEXT,
                pronunciation TEXT, definition TEXT, examples_json TEXT, sense_id TEXT,
                synset_id TEXT, synonyms_json TEXT, cefr_level TEXT, level_source TEXT,
                lexical_field TEXT, source_id TEXT
            );
            CREATE TABLE learning_tasks (
                task_id TEXT, task_type TEXT, instruction TEXT, cefr_level TEXT,
                level_source TEXT, topic TEXT, lesson TEXT, prompt TEXT,
                answer_mode TEXT, correct_answers_json TEXT, options_json TEXT,
                explanation TEXT, source_id TEXT, source_record TEXT, license TEXT,
                attribution TEXT, created_by TEXT, generation_method TEXT, part_of_speech TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO dictionary_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "word-a1", "apple", "apple", "noun", None, "A fruit.", "[]",
                "sense-a1", "synset-a1", "[]", "A1", "CEFR-J exact match",
                "", "cefrj-vocabulary-1.5",
            ),
        )
        connection.execute(
            "INSERT INTO dictionary_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "word-unknown", "apple", "apple", "noun", None, "An example.", "[]",
                "sense-unknown", "synset-unknown", "[]", None, None, "", "oewn-2025",
            ),
        )
        connection.execute(
            "INSERT INTO learning_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-a1", "definition_to_word", "Level A1", "A1", "CEFR-J",
                "Food", None, "A fruit.", "text", json.dumps(["apple"]), "[]",
                "Explanation.", "oewn-2025", "synset-a1", "CC BY 4.0", "Attribution",
                "project", "test fixture", "noun",
            ),
        )
        connection.execute(
            "INSERT INTO learning_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-unknown", "definition_to_word", "Unknown level", None, None,
                "Food", None, "Another example.", "text", json.dumps(["pear"]), "[]",
                "Explanation.", "oewn-2025", "synset-unknown", "CC BY 4.0", "Attribution",
                "project", "test fixture", "noun",
            ),
        )
    return OpenDataset(path)


def test_dictionary_lookup_returns_only_the_requested_cefr_level(
    open_dataset: OpenDataset,
) -> None:
    entries = open_dataset.lookup("APPLE", "A1")

    assert [entry["entry_id"] for entry in entries] == ["word-a1"]
    assert open_dataset.lookup("apple", "A2") == []


def test_task_queries_keep_unknown_levels_separate(open_dataset: OpenDataset) -> None:
    assert open_dataset.task_count("A1") == 1
    assert open_dataset.task_count(None) == 1
    assert open_dataset.task("task-a1")["level"] == "A1"
    assert open_dataset.random_task("A1", {"task-a1"}) is None
    assert open_dataset.random_task("A1", excluded_records={"synset-a1"}) is None
    assert open_dataset.random_task("A1", topic="Food")["topic"] == "Food"
    assert open_dataset.random_task("A1", topic="Travel") is None


def test_invalid_cefr_level_is_rejected(open_dataset: OpenDataset) -> None:
    with pytest.raises(ValueError):
        open_dataset.task_count("A9")
