import json

from telegram_bot.reference_data import ReferenceLibrary


def test_reference_library_keeps_records_and_examples_at_the_selected_level(tmp_path) -> None:
    (tmp_path / "cefrj-vocabulary-profile-1.5.csv").write_text(
        "headword,pos,CEFR\napple,noun,A1\nadvanced,adjective,C2\n",
        encoding="utf-8",
    )
    (tmp_path / "cefrj-grammar-profile-20180315.csv").write_text(
        "Grammatical Item,CEFR-J Level\nPresent simple,A1.1\nInversion,C2.1\n",
        encoding="utf-8",
    )
    (tmp_path / "cefr-sp.json").write_text(
        json.dumps(
            [
                {"lang": "en", "cefr_level": "A1", "text": "I have an apple ."},
                {"lang": "en", "cefr_level": "C2", "text": "The advanced theory is disputed ."},
            ]
        ),
        encoding="utf-8",
    )
    courses = [
        {
            "lessons": [
                {
                    "level": "A1",
                    "vocabulary": [
                        {
                            "word": "apple",
                            "translation": "яблуко",
                            "example": "I have an apple.",
                            "example_translation": "У мене є яблуко.",
                        }
                    ],
                }
            ]
        }
    ]

    reference = ReferenceLibrary.load(tmp_path, courses, enable_cefr_sp=True)

    assert [row["word"] for row in reference.vocabulary["A1"]] == ["apple"]
    assert [row["word"] for row in reference.vocabulary["C2"]] == ["advanced"]
    assert reference.grammar_topic("A1")["item"] == "Present simple"
    assert reference.grammar_topic("C2")["item"] == "Inversion"
    assert reference.sentence("A1")["text"] == "I have an apple."
    assert reference.sentence("A1", contains="advanced") is None
    word, project_word = reference.practice_word("A1")
    assert word["word"] == "apple"
    assert project_word["translation"] == "яблуко"
    assert reference.practice_word("C2") is None
    assert reference.vocabulary["A1"][0]["license"].startswith("Commercial")
    assert reference.sentences["A1"][0]["license"] == "CC BY-NC-SA 4.0"


def test_reference_library_reports_unreadable_files_and_ignores_missing_files(tmp_path) -> None:
    (tmp_path / "cefrj-vocabulary-profile-1.5.csv").write_bytes(b"\xff")

    reference = ReferenceLibrary.load(tmp_path, [], enable_cefr_sp=True)

    assert reference.read_error("cefrj-vocabulary-profile-1.5.csv")
    assert not reference.read_error("cefrj-grammar-profile-20180315.csv")
    assert not reference.read_error("cefr-sp.json")
    assert reference.vocabulary["A1"] == []
