from __future__ import annotations

import csv
import json
import logging
import random
import re
from pathlib import Path
from typing import Any


CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
CEFRJ_SOURCE = "CEFR-J English Vocabulary and Grammar Profiles"
CEFRJ_URL = "https://github.com/openlanguageprofiles/olp-en-cefrj"
CEFRJ_LICENSE = "Commercial and research use with attribution and citation"
CEFRJ_CITATION = (
    "The CEFR-J Wordlist Version 1.5 and CEFR-J Grammar Profile Version 20180315, "
    "Tono Laboratory, Tokyo University of Foreign Studies."
)
OCTANOVE_SOURCE = "Octanove Vocabulary Profile C1/C2 version 1.0"
OCTANOVE_LICENSE = "CC BY-SA 4.0"
OCTANOVE_URL = "https://github.com/openlanguageprofiles/olp-en-cefrj"
CEFR_SP_SOURCE = "CEFR-SP English sentence subset (UniversalCEFR)"
CEFR_SP_LICENSE = "CC BY-NC-SA 4.0"
CEFR_SP_URL = "https://huggingface.co/datasets/UniversalCEFR/cefr_sp_en"
_LEVEL_PREFIX = re.compile(r"^(A1|A2|B1|B2|C1|C2)(?:[.\s]|$)")
_logger = logging.getLogger(__name__)


class ReferenceLibrary:
    def __init__(
        self,
        *,
        vocabulary: dict[str, list[dict[str, str]]],
        grammar: dict[str, list[dict[str, str]]],
        sentences: dict[str, list[dict[str, str]]],
        project_vocabulary: dict[str, dict[str, dict[str, Any]]],
        errors: dict[str, str],
        sentences_enabled: bool,
    ) -> None:
        self.vocabulary = vocabulary
        self.grammar = grammar
        self.sentences = sentences
        self.project_vocabulary = project_vocabulary
        self.errors = errors
        self.sentences_enabled = sentences_enabled

    @classmethod
    def empty(cls) -> ReferenceLibrary:
        return cls(
            vocabulary={level: [] for level in CEFR_LEVELS},
            grammar={level: [] for level in CEFR_LEVELS},
            sentences={level: [] for level in CEFR_LEVELS},
            project_vocabulary={level: {} for level in CEFR_LEVELS},
            errors={},
            sentences_enabled=False,
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        courses: list[dict[str, Any]],
        *,
        enable_cefr_sp: bool = False,
    ) -> ReferenceLibrary:
        vocabulary = {level: [] for level in CEFR_LEVELS}
        grammar = {level: [] for level in CEFR_LEVELS}
        sentences = {level: [] for level in CEFR_LEVELS}
        project_vocabulary = _project_vocabulary(courses)
        errors: dict[str, str] = {}

        _load_vocab_file(
            directory / "cefrj-vocabulary-profile-1.5.csv",
            vocabulary,
            errors,
            source=CEFRJ_SOURCE,
            source_url=CEFRJ_URL,
            license=CEFRJ_LICENSE,
            citation=CEFRJ_CITATION,
            level_field="CEFR",
        )
        _load_vocab_file(
            directory / "octanove-vocabulary-profile-c1c2-1.0.csv",
            vocabulary,
            errors,
            source=OCTANOVE_SOURCE,
            source_url=OCTANOVE_URL,
            license=OCTANOVE_LICENSE,
            citation="Octanove Vocabulary Profile C1/C2 version 1.0.",
            level_field="CEFR",
        )
        _load_grammar_file(directory / "cefrj-grammar-profile-20180315.csv", grammar, errors)
        if enable_cefr_sp:
            _load_sentence_file(directory / "cefr-sp.json", sentences, errors)

        return cls(
            vocabulary=vocabulary,
            grammar=grammar,
            sentences=sentences,
            project_vocabulary=project_vocabulary,
            errors=errors,
            sentences_enabled=enable_cefr_sp,
        )

    def word(self, level: str) -> dict[str, str] | None:
        rows = self.vocabulary.get(level, [])
        return random.choice(rows) if rows else None

    def grammar_topic(self, level: str) -> dict[str, str] | None:
        rows = self.grammar.get(level, [])
        return random.choice(rows) if rows else None

    def sentence(self, level: str, *, contains: str | None = None) -> dict[str, str] | None:
        rows = self.sentences.get(level, [])
        if contains:
            pattern = re.compile(rf"(?<!\w){re.escape(contains)}(?!\w)", re.IGNORECASE)
            rows = [row for row in rows if pattern.search(row["text"])]
        return random.choice(rows) if rows else None

    def practice_word(self, level: str) -> tuple[dict[str, str], dict[str, Any]] | None:
        available = [
            (word, self.project_vocabulary[level][word["word"].casefold()])
            for word in self.vocabulary.get(level, [])
            if word["word"].casefold() in self.project_vocabulary.get(level, {})
        ]
        return random.choice(available) if available else None

    def read_error(self, filename: str) -> bool:
        return filename in self.errors


def _project_vocabulary(courses: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    result = {level: {} for level in CEFR_LEVELS}
    for course in courses:
        for lesson in course.get("lessons", []):
            level = lesson.get("level")
            if level not in result:
                continue
            for word in lesson.get("vocabulary", []):
                if isinstance(word.get("word"), str):
                    result[level].setdefault(word["word"].casefold(), word)
    return result


def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"CSV headers must include: {', '.join(sorted(required))}")
        return list(reader)


def _record_file_error(path: Path, errors: dict[str, str], exc: Exception) -> None:
    errors[path.name] = type(exc).__name__
    _logger.warning("Could not read dataset file %s (%s).", path.name, type(exc).__name__)


def _load_vocab_file(
    path: Path,
    vocabulary: dict[str, list[dict[str, str]]],
    errors: dict[str, str],
    *,
    source: str,
    source_url: str,
    license: str,
    citation: str,
    level_field: str,
) -> None:
    if not path.is_file():
        return
    try:
        rows = _read_csv(path, {"headword", "pos", level_field})
        for row in rows:
            level_value = row.get(level_field)
            word_value = row.get("headword")
            if not isinstance(level_value, str) or not isinstance(word_value, str):
                continue
            level = level_value.strip().upper()
            word = word_value.strip()
            if level not in vocabulary or not word:
                continue
            vocabulary[level].append(
                {
                    "word": word,
                    "part_of_speech": (row.get("pos") or "").strip(),
                    "level": level,
                    "source": source,
                    "source_url": source_url,
                    "license": license,
                    "citation": citation,
                }
            )
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        _record_file_error(path, errors, exc)


def _load_grammar_file(
    path: Path, grammar: dict[str, list[dict[str, str]]], errors: dict[str, str]
) -> None:
    if not path.is_file():
        return
    try:
        rows = _read_csv(path, {"Grammatical Item", "CEFR-J Level"})
        for row in rows:
            level_value = row.get("CEFR-J Level")
            item_value = row.get("Grammatical Item")
            if not isinstance(level_value, str) or not isinstance(item_value, str):
                continue
            match = _LEVEL_PREFIX.match(level_value.strip().upper())
            item = item_value.strip()
            if not match or not item:
                continue
            level = match.group(1)
            grammar[level].append(
                {
                    "item": item,
                    "level": level,
                    "cefrj_level": level_value.strip(),
                    "source": CEFRJ_SOURCE,
                    "source_url": CEFRJ_URL,
                    "license": CEFRJ_LICENSE,
                    "citation": CEFRJ_CITATION,
                }
            )
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        _record_file_error(path, errors, exc)


def _load_sentence_file(
    path: Path, sentences: dict[str, list[dict[str, str]]], errors: dict[str, str]
) -> None:
    if not path.is_file():
        return
    try:
        with path.open(encoding="utf-8") as stream:
            rows = json.load(stream)
        if not isinstance(rows, list):
            raise ValueError("CEFR-SP JSON must contain a list of sentence records.")
        for row in rows:
            if not isinstance(row, dict) or row.get("lang") != "en":
                continue
            level = row.get("cefr_level")
            text = row.get("text")
            license = row.get("license")
            if (
                not isinstance(level, str)
                or level not in sentences
                or not isinstance(text, str)
                or not text.strip()
            ):
                continue
            sentences[level].append(
                {
                    "text": re.sub(r"\s+([?.!,;:])", r"\1", text.strip()),
                    "level": level,
                    "source": CEFR_SP_SOURCE,
                    "source_url": CEFR_SP_URL,
                    "license": license if isinstance(license, str) else CEFR_SP_LICENSE,
                    "citation": (
                        "Arase, Uchida & Kajiwara (2022), CEFR-Based Sentence-Difficulty "
                        "Annotation and Assessment, EMNLP."
                    ),
                }
            )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _record_file_error(path, errors, exc)
