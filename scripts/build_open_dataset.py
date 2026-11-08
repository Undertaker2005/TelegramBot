from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "data" / "downloads" / "english-wordnet-2025-json.zip"
DEFAULT_DATASET_DIR = ROOT / "data" / "english_cefr_datasets"
DEFAULT_DATABASE = ROOT / "data" / "open_training.sqlite3"
REPORT_PATH = ROOT / "content" / "dataset_report.json"
REGISTRY_PATH = ROOT / "content" / "source_registry.json"
TASK_TARGET = 5_001
TASK_TYPES = (
    "definition_to_word",
    "word_to_definition",
    "synonym_choice",
)
POS_NAMES = {
    "n": "noun",
    "v": "verb",
    "a": "adjective",
    "s": "adjective",
    "r": "adverb",
}
SOURCE_REGISTRY = [
    {
        "id": "oewn-2025",
        "name": "Open English WordNet 2025 core edition",
        "url": "https://github.com/globalwordnet/english-wordnet",
        "download_url": "https://en-word.net/static/english-wordnet-2025-json.zip",
        "license": "CC BY 4.0; underlying Princeton WordNet license also applies",
        "restrictions": (
            "Retain the Open English WordNet and Princeton WordNet notices and attribution. "
            "Do not use Princeton's name in advertising or imply endorsement. "
            "No Ukrainian translations or CEFR labels are supplied by this dataset."
        ),
        "attribution": (
            "Open English WordNet Team; data derived from Princeton WordNet. "
            "McCrae et al. (2019), English WordNet 2019 – An Open-Source WordNet for English."
        ),
        "downloaded": True,
    },
    {
        "id": "cefrj-vocabulary-1.5",
        "name": "CEFR-J English Vocabulary Profile 1.5",
        "url": "https://github.com/openlanguageprofiles/olp-en-cefrj",
        "download_url": (
            "https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/"
            "master/cefrj-vocabulary-profile-1.5.csv"
        ),
        "license": "Commercial and research use with attribution and citation; terms in source README",
        "restrictions": "Copyright belongs to Tono Laboratory, Tokyo University of Foreign Studies.",
        "attribution": (
            "The CEFR-J Wordlist Version 1.5. Compiled by Yukio Tono, "
            "Tokyo University of Foreign Studies."
        ),
        "downloaded": True,
    },
    {
        "id": "cefrj-grammar-20180315",
        "name": "CEFR-J English Grammar Profile 20180315",
        "url": "https://github.com/openlanguageprofiles/olp-en-cefrj",
        "download_url": (
            "https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/"
            "master/cefrj-grammar-profile-20180315.csv"
        ),
        "license": "Commercial and research use with attribution and citation; terms in source README",
        "restrictions": (
            "Copyright belongs to Tono Laboratory, Tokyo University of Foreign Studies. "
            "The grammar profile is only partially translated from Japanese."
        ),
        "attribution": (
            "The CEFR-J Grammar Profile Version 20180315. "
            "Tono Laboratory, Tokyo University of Foreign Studies."
        ),
        "downloaded": True,
    },
    {
        "id": "octanove-vocabulary-c1c2-1.0",
        "name": "Octanove Vocabulary Profile C1/C2 version 1.0",
        "url": "https://github.com/openlanguageprofiles/olp-en-cefrj",
        "download_url": (
            "https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/"
            "master/octanove-vocabulary-profile-c1c2-1.0.csv"
        ),
        "license": "CC BY-SA 4.0",
        "restrictions": "Adapted material must retain attribution and be shared under the same license.",
        "attribution": "Octanove Vocabulary Profile C1/C2 version 1.0.",
        "downloaded": True,
    },
    {
        "id": "cefr-sp-excluded",
        "name": "CEFR-SP English sentence subset",
        "url": "https://huggingface.co/datasets/UniversalCEFR/cefr_sp_en",
        "download_url": None,
        "license": "CC BY-NC-SA 4.0",
        "restrictions": "Non-commercial only; not downloaded or included in the commercial-capable dataset.",
        "attribution": "Arase, Uchida & Kajiwara (2022), CEFR-Based Sentence-Difficulty Annotation and Assessment.",
        "downloaded": False,
    },
]


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _display(value: str) -> str:
    return re.sub(r"_+", " ", value).strip()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _read_wordnet(archive: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], int]:
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        entries: dict[str, dict[str, Any]] = {}
        synsets: dict[str, tuple[dict[str, Any], str]] = {}
        for name in names:
            if name.startswith("entries-") and name.endswith(".json"):
                part = json.loads(source.read(name))
                for lemma, poses in part.items():
                    entries.setdefault(lemma, {}).update(poses)
            elif name.endswith(".json") and not name.startswith("frames"):
                part = json.loads(source.read(name))
                for synset_id, record in part.items():
                    synsets[synset_id] = (record, Path(name).stem)

    senses: list[dict[str, Any]] = []
    duplicate_senses = 0
    seen: set[tuple[str, str, str]] = set()
    for raw_lemma, poses in entries.items():
        lemma = _display(raw_lemma)
        if not lemma:
            continue
        for pos, entry in poses.items():
            for sense in entry.get("sense", []):
                synset_id = sense.get("synset")
                key = (_norm(lemma), pos, str(synset_id))
                if key in seen:
                    duplicate_senses += 1
                    continue
                seen.add(key)
                found = synsets.get(str(synset_id))
                if not found:
                    raise ValueError(f"Open English WordNet sense references missing synset {synset_id!r}.")
                synset, lexical_file = found
                definition = " ".join(synset.get("definition", [])).strip()
                if not definition:
                    raise ValueError(f"Open English WordNet synset {synset_id!r} has no definition.")
                senses.append(
                    {
                        "entry_id": f"oewn2025:{synset_id}:{pos}:{_norm(lemma)}",
                        "lemma": lemma,
                        "lemma_norm": _norm(lemma),
                        "pos": POS_NAMES.get(pos, pos),
                        "pronunciation": _first_pronunciation(entry),
                        "definition": definition,
                        "examples": synset.get("example", []),
                        "sense_id": sense.get("id", ""),
                        "synset_id": str(synset_id),
                        "synonyms": [_display(item) for item in synset.get("members", [])],
                        "cefr_level": None,
                        "level_source": None,
                        "lexical_field": lexical_file,
                        "source_id": "oewn-2025",
                    }
                )
    return synsets, senses, duplicate_senses


def _first_pronunciation(entry: dict[str, Any]) -> str | None:
    for pronunciation in entry.get("pronunciation", []):
        value = pronunciation.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _apply_cefr_levels(senses: list[dict[str, Any]], cefr_rows: list[dict[str, str]]) -> None:
    by_lemma_pos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in cefr_rows:
        headword = row.get("headword", "").strip()
        level = row.get("CEFR", "").strip().upper()
        pos = row.get("pos", "").strip().casefold()
        if not headword or level not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
            continue
        for component in re.split(r"\s*/\s*", headword):
            by_lemma_pos[(_norm(component), pos)].add(level)
    pos_aliases = {
        "noun": {"noun"},
        "verb": {"verb"},
        "adjective": {"adjective"},
        "adverb": {"adverb"},
    }
    for record in senses:
        levels = by_lemma_pos.get((record["lemma_norm"], record["pos"]), set())
        if len(levels) == 1 and record["pos"] in pos_aliases:
            record["cefr_level"] = next(iter(levels))
            record["level_source"] = "cefrj-vocabulary-1.5"


def _task(
    *,
    task_type: str,
    synset_id: str,
    pos: str,
    prompt: str,
    accepted: list[str],
    options: list[str],
    explanation: str,
    topic: str,
    answer_mode: str,
    cefr_level: str | None,
) -> dict[str, Any]:
    task_id = f"oewn25-{task_type}-{synset_id}"
    return {
        "task_id": task_id,
        "type": task_type,
        "instruction": (
            f"Рівень CEFR {cefr_level} зіставлено за точною лемою та частиною мови."
            if cefr_level
            else "Рівень CEFR для цього завдання невідомий."
        ),
        "level": cefr_level,
        "level_source": "CEFR-J exact word/POS match" if cefr_level else None,
        "topic": topic,
        "lesson": None,
        "prompt": prompt,
        "answer_mode": answer_mode,
        "correct_answers": accepted,
        "options": options,
        "explanation": explanation,
        "source_id": "oewn-2025",
        "source_record": synset_id,
        "license": "CC BY 4.0; underlying Princeton WordNet license also applies",
        "attribution": (
            "Open English WordNet Team; data derived from Princeton WordNet. "
            "https://github.com/globalwordnet/english-wordnet"
        ),
        "created_by": "Original exercise created by this project's dataset builder",
        "generation_method": f"{task_type}; definition/synonyms derived from the cited synset.",
        "pos": pos,
    }


def _build_exercises(
    synsets: dict[str, dict[str, Any]],
    senses: list[dict[str, Any]],
    target: int = TASK_TARGET,
) -> list[dict[str, Any]]:
    levels_by_synset: dict[str, set[str]] = defaultdict(set)
    for record in senses:
        if record["cefr_level"]:
            levels_by_synset[record["synset_id"]].add(record["cefr_level"])

    records: list[dict[str, Any]] = []
    for synset_id, (raw, lexical_field) in synsets.items():
        definition = " ".join(raw.get("definition", [])).strip()
        members = sorted(
            {_display(item) for item in raw.get("members", []) if _display(item)},
            key=lambda item: (len(item), item.casefold()),
        )
        pos_code = raw.get("partOfSpeech", "")
        pos = POS_NAMES.get(pos_code, pos_code)
        if not definition or not members or not pos:
            continue
        definition_norm = _norm(definition)
        if not 8 <= len(definition) <= 300 or not definition_norm:
            continue
        if any(re.search(rf"(?<!\w){re.escape(member)}(?!\w)", definition, re.IGNORECASE) for member in members):
            definition_text_ok = False
        else:
            definition_text_ok = True
        records.append(
            {
                "synset_id": synset_id,
                "definition": definition,
                "definition_norm": definition_norm,
                "members": members,
                "pos": pos,
                "examples": raw.get("example", []),
                "lexical_field": lexical_field,
                "definition_text_ok": definition_text_ok,
                "cefr_level": (
                    next(iter(levels_by_synset[synset_id]))
                    if len(levels_by_synset[synset_id]) == 1
                    else None
                ),
            }
        )
    records.sort(key=lambda item: hashlib.sha256(item["synset_id"].encode()).hexdigest())

    by_pos = defaultdict(list)
    for record in records:
        by_pos[record["pos"]].append(record)
    for pos_records in by_pos.values():
        pos_records.sort(key=lambda item: hashlib.sha256(item["synset_id"].encode()).hexdigest())

    result: list[dict[str, Any]] = []
    used_content: set[str] = set()
    used_ids: set[str] = set()
    per_type = target // len(TASK_TYPES)
    counts: Counter[str] = Counter()

    def append(candidate: dict[str, Any]) -> bool:
        content_key = _norm(
            candidate["prompt"] + " " + " ".join(candidate["options"])
        )
        if content_key in used_content:
            return False
        if candidate["task_id"] in used_ids:
            return False
        if not candidate["correct_answers"] or not all(
            isinstance(answer, str) and answer.strip()
            for answer in candidate["correct_answers"]
        ):
            return False
        if candidate["answer_mode"] == "choice":
            correct_options = [
                option for option in candidate["options"]
                if _norm(option) in {_norm(answer) for answer in candidate["correct_answers"]}
            ]
            if len(candidate["options"]) < 2 or len(correct_options) != 1:
                return False
        result.append(candidate)
        used_content.add(content_key)
        used_ids.add(candidate["task_id"])
        return True

    for record in records:
        if counts["definition_to_word"] >= per_type:
            break
        if not record["definition_text_ok"]:
            continue
        accepted = record["members"]
        if append(
            _task(
                task_type="definition_to_word",
                synset_id=record["synset_id"],
                pos=record["pos"],
                prompt=(
                    f"Введіть англійське слово або вираз за визначенням "
                    f"({record['pos']}): {record['definition']}"
                ),
                accepted=accepted,
                options=[],
                explanation=(
                    f"Визначення Open English WordNet: {record['definition']} "
                    f"Допустимі синоніми: {', '.join(accepted)}."
                ),
                topic=record["lexical_field"],
                answer_mode="text",
                cefr_level=record["cefr_level"],
            )
        ):
            counts["definition_to_word"] += 1

    for record in records:
        if counts["word_to_definition"] >= per_type:
            break
        target_word = record["members"][0]
        distractors: list[dict[str, Any]] = []
        seen_definitions = {record["definition_norm"]}
        for other in by_pos[record["pos"]]:
            if other["synset_id"] == record["synset_id"] or other["definition_norm"] in seen_definitions:
                continue
            if set(map(_norm, other["members"])) & set(map(_norm, record["members"])):
                continue
            distractors.append(other)
            seen_definitions.add(other["definition_norm"])
            if len(distractors) == 3:
                break
        if len(distractors) < 3:
            continue
        options = [record["definition"], *(item["definition"] for item in distractors)]
        random.Random(f"oewn25-word-to-definition-{record['synset_id']}").shuffle(options)
        if append(
            _task(
                task_type="word_to_definition",
                synset_id=record["synset_id"],
                pos=record["pos"],
                prompt=f"Оберіть визначення слова “{target_word}” ({record['pos']}):",
                accepted=[record["definition"]],
                options=options,
                explanation=f"“{target_word}”: {record['definition']}",
                topic=record["lexical_field"],
                answer_mode="choice",
                cefr_level=record["cefr_level"],
            )
        ):
            counts["word_to_definition"] += 1

    for record in records:
        if counts["synonym_choice"] >= per_type:
            break
        if len(record["members"]) < 2:
            continue
        target_word, correct_word = record["members"][:2]
        distractors: list[str] = []
        for other in by_pos[record["pos"]]:
            if other["synset_id"] == record["synset_id"]:
                continue
            if set(map(_norm, other["members"])) & set(map(_norm, record["members"])):
                continue
            candidate = next((item for item in other["members"] if _norm(item) != _norm(target_word)), None)
            if candidate and _norm(candidate) not in {_norm(x) for x in distractors}:
                distractors.append(candidate)
            if len(distractors) == 3:
                break
        if len(distractors) < 3:
            continue
        options = [correct_word, *distractors]
        random.Random(f"oewn25-synonym-choice-{record['synset_id']}").shuffle(options)
        if append(
            _task(
                task_type="synonym_choice",
                synset_id=record["synset_id"],
                pos=record["pos"],
                prompt=f"Оберіть синонім до англійського слова “{target_word}”:",
                accepted=[correct_word],
                options=options,
                explanation=(
                    f"“{target_word}” і “{correct_word}” є членами одного синонімічного набору "
                    "Open English WordNet."
                ),
                topic=record["lexical_field"],
                answer_mode="choice",
                cefr_level=record["cefr_level"],
            )
        ):
            counts["synonym_choice"] += 1

    counts = Counter(item["type"] for item in result)
    if len(result) != target or any(counts[task_type] != per_type for task_type in TASK_TYPES):
        raise ValueError(
            f"Could not build {target} balanced unique exercises; got {len(result)} "
            f"({dict(counts)})."
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def build(
    archive: Path,
    dataset_dir: Path,
    database_path: Path,
    task_target: int = TASK_TARGET,
) -> dict[str, Any]:
    vocab_path = dataset_dir / "cefrj-vocabulary-profile-1.5.csv"
    grammar_path = dataset_dir / "cefrj-grammar-profile-20180315.csv"
    octanove_path = dataset_dir / "octanove-vocabulary-profile-c1c2-1.0.csv"
    required = (archive, vocab_path, grammar_path, octanove_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Required verified dataset files are missing: " + ", ".join(missing))

    retrieval_date = datetime.now(UTC).date().isoformat()
    synsets, senses, duplicate_senses = _read_wordnet(archive)
    cefr_rows = _read_csv(vocab_path)
    grammar_rows = _read_csv(grammar_path)
    octanove_rows = _read_csv(octanove_path)
    _apply_cefr_levels(senses, cefr_rows + octanove_rows)
    exercises = _build_exercises(synsets, senses, task_target)

    vocabulary_by_level = Counter(
        row.get("CEFR", "").strip().upper()
        for row in cefr_rows + octanove_rows
        if row.get("CEFR", "").strip().upper() in {"A1", "A2", "B1", "B2", "C1", "C2"}
    )
    grammar_by_level = Counter()
    topics: Counter[str] = Counter()
    for row in grammar_rows:
        match = re.match(r"^(A1|A2|B1|B2|C1|C2)(?:[.\s]|$)", row.get("CEFR-J Level", "").strip().upper())
        if match:
            grammar_by_level[match.group(1)] += 1
    for row in cefr_rows + octanove_rows:
        for field in ("CoreInventory 1", "CoreInventory 2", "Threshold"):
            value = row.get(field, "").strip()
            if value:
                topics[value] += 1

    unique_word_forms = {_norm(row["lemma"]) for row in senses}
    profile_words: set[str] = set()
    profile_keys: dict[str, set[tuple[str, str, str]]] = {
        "cefrj-vocabulary-1.5": set(),
        "octanove-vocabulary-c1c2-1.0": set(),
    }
    for source_id, rows in (
        ("cefrj-vocabulary-1.5", cefr_rows),
        ("octanove-vocabulary-c1c2-1.0", octanove_rows),
    ):
        for row in rows:
            headword = row.get("headword", "").strip()
            level = row.get("CEFR", "").strip().upper()
            pos = row.get("pos", "").strip().casefold()
            if not headword or level not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
                continue
            profile_keys[source_id].add((_norm(headword), pos, level))
            profile_words.update(
                _norm(component) for component in re.split(r"\s*/\s*", headword)
            )
    combined_unique_lemmas = unique_word_forms | profile_words
    unique_ids = {row["entry_id"] for row in senses}
    cefr_level_matches = sum(row["cefr_level"] is not None for row in senses)
    task_counts = Counter(row["type"] for row in exercises)
    exercise_levels = Counter(row["level"] or "unknown" for row in exercises)
    duplicate_task_content = len(exercises) - len({
        _norm(item["prompt"] + " " + " ".join(item["options"]))
        for item in exercises
    })
    if duplicate_task_content:
        raise ValueError(f"Generated tasks contain {duplicate_task_content} duplicate content fingerprints.")

    sources = []
    source_counts = {
        "oewn-2025": len(senses),
        "cefrj-vocabulary-1.5": len(profile_keys["cefrj-vocabulary-1.5"]),
        "cefrj-grammar-20180315": len(grammar_rows),
        "octanove-vocabulary-c1c2-1.0": len(profile_keys["octanove-vocabulary-c1c2-1.0"]),
        "cefr-sp-excluded": 0,
    }
    paths_by_source = {
        "oewn-2025": archive,
        "cefrj-vocabulary-1.5": vocab_path,
        "cefrj-grammar-20180315": grammar_path,
        "octanove-vocabulary-c1c2-1.0": octanove_path,
    }
    for source in SOURCE_REGISTRY:
        item = {
            **source,
            "retrieved_on": retrieval_date if source["downloaded"] else None,
            "records_imported": source_counts[source["id"]],
        }
        if source["id"] in paths_by_source:
            item["sha256"] = _sha256(paths_by_source[source["id"]])
        sources.append(item)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "counts": {
            "open_english_wordnet_sense_records": len(senses),
            "open_english_wordnet_distinct_lemmas": len(unique_word_forms),
            "open_english_wordnet_distinct_senses": len(synsets),
            "open_english_wordnet_unique_record_ids": len(unique_ids),
            "open_english_wordnet_cefrj_exact_level_matches": cefr_level_matches,
            "cefrj_vocabulary_source_rows": len(cefr_rows),
            "cefrj_vocabulary_unique_imported_records": len(profile_keys["cefrj-vocabulary-1.5"]),
            "octanove_vocabulary_source_rows": len(octanove_rows),
            "octanove_vocabulary_unique_imported_records": len(profile_keys["octanove-vocabulary-c1c2-1.0"]),
            "cefrj_grammar_source_rows": len(grammar_rows),
            "cefrj_grammar_rows_with_level": sum(grammar_by_level.values()),
            "cefrj_vocabulary_duplicate_rows_skipped": (
                len(cefr_rows) - len(profile_keys["cefrj-vocabulary-1.5"])
            ),
            "octanove_vocabulary_duplicate_rows_skipped": (
                len(octanove_rows) - len(profile_keys["octanove-vocabulary-c1c2-1.0"])
            ),
            "generated_unique_exercises": len(exercises),
            "generated_exercises_by_type": dict(sorted(task_counts.items())),
            "generated_exercises_by_cefr": dict(sorted(exercise_levels.items())),
            "combined_unique_dictionary_lemmas": len(combined_unique_lemmas),
        },
        "levels": {
            "vocabulary_source_rows": dict(sorted(vocabulary_by_level.items())),
            "grammar_source_rows_with_level": dict(sorted(grammar_by_level.items())),
        },
        "topics": dict(topics.most_common(100)),
        "duplicates": {
            "duplicate_wordnet_sense_records_skipped": duplicate_senses,
            "duplicate_generated_task_content": duplicate_task_content,
            "generated_task_ids_are_unique": len({item["task_id"] for item in exercises}) == len(exercises),
            "wordnet_definitions_are_sense_specific": True,
        },
        "validation": {
            "wordnet_records_have_definition_and_source": all(
                row["definition"].strip() and row["source_id"] == "oewn-2025" for row in senses
            ),
            "generated_tasks_have_answers_and_explanations": all(
                row["correct_answers"] and row["explanation"].strip() and row["source_id"]
                for row in exercises
            ),
            "multiple_choice_has_exactly_one_correct_option": all(
                sum(
                    _norm(option) in {_norm(answer) for answer in row["correct_answers"]}
                    for option in row["options"]
                ) == 1
                for row in exercises
                if row["answer_mode"] == "choice"
            ),
            "generated_task_level_claims": (
                "CEFR-J exact word/POS matches only; otherwise level is unknown"
            ),
            "human_review": "not performed; generated tasks are structurally validated, not pedagogically certified",
            "errors": [],
        },
        "targets": {
            "dictionary_records_minimum": 50_000,
            "unique_exercises_minimum": 5_000,
            "dictionary_records_met": len(senses) >= 50_000,
            "unique_exercises_met": len(exercises) >= 5_000,
            "dictionary_record_shortfall": max(0, 50_000 - len(senses)),
            "exercise_shortfall": max(0, 5_000 - len(exercises)),
        },
        "notes": [
            "WordNet and CEFR-J do not supply Ukrainian translations; translations are not invented.",
            "WordNet-derived records without an exact CEFR-J match are labeled CEFR unknown.",
            "The generated exercises are original prompts based on Open English WordNet definitions and synsets; they are not source-authored questions or official exam content.",
            "Automated checks verify structure, answers, option uniqueness and content deduplication; human pedagogical review remains outstanding.",
            "CEFR-SP is excluded because its CC BY-NC-SA 4.0 terms are not suitable for a potentially commercial dataset.",
        ],
    }

    database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{database_path.stem}-", suffix=".sqlite3", dir=database_path.parent, delete=False
    ) as stream:
        temporary_db = Path(stream.name)
    try:
        connection = sqlite3.connect(temporary_db)
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA foreign_keys = ON;
            CREATE TABLE dictionary_entries (
                entry_id TEXT PRIMARY KEY,
                lemma TEXT NOT NULL,
                lemma_norm TEXT NOT NULL,
                part_of_speech TEXT NOT NULL,
                pronunciation TEXT,
                definition TEXT NOT NULL,
                examples_json TEXT NOT NULL,
                sense_id TEXT NOT NULL,
                synset_id TEXT NOT NULL,
                synonyms_json TEXT NOT NULL,
                cefr_level TEXT,
                level_source TEXT,
                lexical_field TEXT NOT NULL,
                source_id TEXT NOT NULL
            );
            CREATE INDEX idx_dictionary_lemma ON dictionary_entries(lemma_norm, cefr_level);
            CREATE INDEX idx_dictionary_level ON dictionary_entries(cefr_level, lemma_norm);
            CREATE TABLE cefr_vocabulary (
                entry_id TEXT PRIMARY KEY,
                headword TEXT NOT NULL,
                part_of_speech TEXT NOT NULL,
                cefr_level TEXT NOT NULL,
                topic TEXT NOT NULL,
                source_id TEXT NOT NULL
            );
            CREATE INDEX idx_cefr_vocab_level ON cefr_vocabulary(cefr_level, headword);
            CREATE TABLE cefr_grammar (
                entry_id TEXT PRIMARY KEY,
                item TEXT NOT NULL,
                cefr_level TEXT,
                shorthand TEXT,
                source_id TEXT NOT NULL
            );
            CREATE TABLE learning_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                instruction TEXT NOT NULL,
                cefr_level TEXT,
                level_source TEXT,
                topic TEXT NOT NULL,
                lesson TEXT,
                prompt TEXT NOT NULL,
                answer_mode TEXT NOT NULL,
                correct_answers_json TEXT NOT NULL,
                options_json TEXT NOT NULL,
                explanation TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_record TEXT NOT NULL,
                license TEXT NOT NULL,
                attribution TEXT NOT NULL,
                created_by TEXT NOT NULL,
                generation_method TEXT NOT NULL,
                part_of_speech TEXT NOT NULL
            );
            CREATE INDEX idx_learning_tasks_type ON learning_tasks(task_type, cefr_level);
            CREATE TABLE dataset_sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                license TEXT NOT NULL,
                attribution TEXT NOT NULL,
                retrieved_on TEXT,
                record_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO dictionary_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["entry_id"], row["lemma"], row["lemma_norm"], row["pos"], row["pronunciation"],
                    row["definition"], json.dumps(row["examples"], ensure_ascii=False), row["sense_id"],
                    row["synset_id"], json.dumps(row["synonyms"], ensure_ascii=False),
                    row["cefr_level"], row["level_source"], row["lexical_field"], row["source_id"],
                )
                for row in senses
            ],
        )
        cefr_vocab_records = []
        for source_id, rows in (
            ("cefrj-vocabulary-1.5", cefr_rows),
            ("octanove-vocabulary-c1c2-1.0", octanove_rows),
        ):
            seen: set[tuple[str, str, str]] = set()
            for index, row in enumerate(rows):
                headword = row.get("headword", "").strip()
                level = row.get("CEFR", "").strip().upper()
                pos = row.get("pos", "").strip()
                key = (_norm(headword), pos.casefold(), level)
                if not headword or level not in {"A1", "A2", "B1", "B2", "C1", "C2"} or key in seen:
                    continue
                seen.add(key)
                topics_for_row = [
                    row.get(field, "").strip()
                    for field in ("CoreInventory 1", "CoreInventory 2", "Threshold")
                    if row.get(field, "").strip()
                ]
                cefr_vocab_records.append(
                    (
                        f"{source_id}:{index}:{_norm(headword)}:{level}",
                        headword, pos, level, " | ".join(topics_for_row), source_id,
                    )
                )
        connection.executemany("INSERT INTO cefr_vocabulary VALUES (?, ?, ?, ?, ?, ?)", cefr_vocab_records)
        grammar_records = []
        for index, row in enumerate(grammar_rows):
            item = row.get("Grammatical Item", "").strip()
            if not item:
                continue
            match = re.match(r"^(A1|A2|B1|B2|C1|C2)(?:[.\s]|$)", row.get("CEFR-J Level", "").strip().upper())
            grammar_records.append(
                (
                    f"cefrj-grammar-20180315:{index}", item,
                    match.group(1) if match else None, row.get("Shorthand Code", "").strip(),
                    "cefrj-grammar-20180315",
                )
            )
        connection.executemany("INSERT INTO cefr_grammar VALUES (?, ?, ?, ?, ?)", grammar_records)
        connection.executemany(
            "INSERT INTO learning_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["task_id"], row["type"], row["instruction"], row["level"],
                    row["level_source"], row["topic"],
                    row["lesson"], row["prompt"], row["answer_mode"],
                    json.dumps(row["correct_answers"], ensure_ascii=False),
                    json.dumps(row["options"], ensure_ascii=False), row["explanation"],
                    row["source_id"], row["source_record"], row["license"], row["attribution"],
                    row["created_by"], row["generation_method"], row["pos"],
                )
                for row in exercises
            ],
        )
        connection.executemany(
            "INSERT INTO dataset_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["id"], item["name"], item["url"], item["license"], item["attribution"],
                    item["retrieved_on"], item["records_imported"],
                    json.dumps(item, ensure_ascii=False),
                )
                for item in sources
            ],
        )
        connection.commit()
        connection.execute("PRAGMA optimize")
        connection.close()
        os.replace(temporary_db, database_path)
    except Exception:
        if temporary_db.exists():
            temporary_db.unlink()
        raise

    _atomic_json(REPORT_PATH, report)
    _atomic_json(
        REGISTRY_PATH,
        {
            "as_of": retrieval_date,
            "sources": sources,
            "license_policy": (
                "Only the downloaded sources marked commercial/research use or CC BY/CC BY-SA are "
                "included. Non-commercial-only CEFR-SP is documented but excluded."
            ),
        },
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the licensed local English-learning dataset.")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--task-target", type=int, default=TASK_TARGET)
    args = parser.parse_args()
    if args.task_target < 5_000 or args.task_target % len(TASK_TYPES):
        raise SystemExit("--task-target must be at least 5000 and divisible by 3.")
    report = build(args.archive, args.dataset_dir, args.database, args.task_target)
    counts = report["counts"]
    print(
        f"Built {args.database}: {counts['open_english_wordnet_sense_records']} WordNet sense records, "
        f"{counts['cefrj_vocabulary_source_rows']} CEFR-J vocabulary rows, "
        f"{counts['octanove_vocabulary_source_rows']} Octanove rows, "
        f"{counts['generated_unique_exercises']} unique generated exercises."
    )
    print(f"Report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
