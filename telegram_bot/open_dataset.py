from __future__ import annotations

import json
from pathlib import Path
import random
import re
import sqlite3
import unicodedata
from typing import Any


CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def normalize_lookup(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _validate_level(level: str | None) -> None:
    if level is not None and level not in CEFR_LEVELS:
        raise ValueError(f"Unsupported CEFR level: {level}")


class OpenDataset:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def lookup(self, query: str, level: str, limit: int = 5) -> list[dict[str, Any]]:
        _validate_level(level)
        if limit < 1:
            raise ValueError("Lookup limit must be positive.")
        normalized = normalize_lookup(query)
        if not normalized:
            return []
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT entry_id, lemma, part_of_speech, pronunciation, definition,
                       examples_json, synset_id, synonyms_json, cefr_level, level_source,
                       lexical_field, source_id
                FROM dictionary_entries
                WHERE lemma_norm LIKE ?
                  AND cefr_level = ?
                ORDER BY CASE WHEN lemma_norm = ? THEN 0 ELSE 1 END,
                         CASE WHEN cefr_level = ? THEN 0 ELSE 1 END,
                         part_of_speech, synset_id
                LIMIT ?
                """,
                (normalized + "%", level, normalized, level, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "examples": json.loads(row["examples_json"]),
                "synonyms": json.loads(row["synonyms_json"]),
            }
            for row in rows
        ]

    def task_count(self, level: str | None) -> int:
        _validate_level(level)
        clause = "cefr_level IS NULL" if level is None else "cefr_level = ?"
        parameters: tuple[Any, ...] = () if level is None else (level,)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM learning_tasks WHERE {clause}",
                parameters,
            ).fetchone()
        return int(row[0])

    def task(self, task_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM learning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._decode_task(row) if row else None

    def random_task(
        self,
        level: str | None,
        excluded_ids: set[str] | None = None,
        topic: str | None = None,
        excluded_records: set[str] | None = None,
    ) -> dict[str, Any] | None:
        _validate_level(level)
        clauses = ["cefr_level IS NULL" if level is None else "cefr_level = ?"]
        parameters: list[Any] = [] if level is None else [level]
        if topic is not None:
            clauses.append("topic = ?")
            parameters.append(topic)
        if excluded_records:
            ordered_records = sorted(excluded_records)
            clauses.append(
                f"source_record NOT IN ({', '.join('?' for _ in ordered_records)})"
            )
            parameters.extend(ordered_records)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM learning_tasks WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchall()
        excluded = excluded_ids or set()
        available = [row for row in rows if row["task_id"] not in excluded]
        if not available:
            return None
        return self._decode_task(random.choice(available))

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["correct_answers"] = json.loads(task.pop("correct_answers_json"))
        task["options"] = json.loads(task.pop("options_json"))
        task["type"] = task.pop("task_type")
        task["level"] = task.pop("cefr_level")
        return task
