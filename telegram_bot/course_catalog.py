from __future__ import annotations

import random
import re
from typing import Any


_SLUG = re.compile(r"[^a-z0-9]+")
_UK_PARTS = {
    "noun": "іменник",
    "verb": "дієслово",
    "adjective": "прикметник",
}
_GENERAL_NOUN_EXAMPLES = {
    "house": (
        "They bought a ____ with a garden and a front door.",
        "Вони купили будинок із садом і вхідними дверима.",
    ),
    "apartment": (
        "We rent a two-room ____ in a block of flats.",
        "Ми орендуємо двокімнатну квартиру у багатоквартирному будинку.",
    ),
    "room": (
        "There is a window in every ____ of this apartment.",
        "У цій квартирі в кожній кімнаті є вікно.",
    ),
    "kitchen": (
        "I made breakfast in the ____.",
        "Я приготував сніданок на кухні.",
    ),
    "bedroom": (
        "She sleeps in the ____ and cooks in the kitchen.",
        "Вона спить у спальні, а готує на кухні.",
    ),
    "bathroom": (
        "Please wash your hands in the ____.",
        "Будь ласка, помий руки у ванній кімнаті.",
    ),
    "door": (
        "Someone is knocking on the ____.",
        "Хтось стукає у двері.",
    ),
    "window": (
        "Open the ____ to let in some fresh air.",
        "Відчини вікно, щоб впустити свіже повітря.",
    ),
    "floor": (
        "The keys fell onto the ____.",
        "Ключі впали на підлогу.",
    ),
    "ceiling": (
        "A bright lamp hangs from the ____.",
        "Яскрава лампа висить на стелі.",
    ),
    "wall": (
        "We put a family photo on the ____.",
        "Ми повісили сімейне фото на стіну.",
    ),
    "key": (
        "I can't open the door without the ____.",
        "Я не можу відчинити двері без ключа.",
    ),
    "table": (
        "We set the plates on the ____ before dinner.",
        "Перед вечерею ми поставили тарілки на стіл.",
    ),
    "chair": (
        "Please pull up a ____ and sit down.",
        "Підсунь стілець і сідай, будь ласка.",
    ),
    "sofa": (
        "They watched a film while sitting on the ____.",
        "Вони дивилися фільм, сидячи на дивані.",
    ),
    "bed": (
        "The child went to ____ at nine o'clock.",
        "Дитина лягла спати о дев'ятій.",
    ),
    "shelf": (
        "I put the books back on the ____.",
        "Я повернув книжки на полицю.",
    ),
    "mirror": (
        "She looked at herself in the ____.",
        "Вона подивилася на себе у дзеркало.",
    ),
    "garden": (
        "They grow flowers in their ____.",
        "Вони вирощують квіти у своєму саду.",
    ),
    "neighbor": (
        "Our ____ lives in the house next door.",
        "Наш сусід живе у сусідньому будинку.",
    ),
}
_GENERAL_NOUN_TEMPLATES = (
    ("We talked about the word {word} after class.", "Після заняття ми говорили про слово «{translation}»."),
    ("Can you find the word {word} on this list?", "Можеш знайти слово «{translation}» у цьому списку?"),
    ("The teacher wrote the word {word} on the board.", "Учитель написав слово «{translation}» на дошці."),
    ("I saw the word {word} in today's lesson.", "Я побачив слово «{translation}» на сьогоднішньому уроці."),
    ("Let's learn the word {word} together.", "Вивчімо слово «{translation}» разом."),
    ("She used the word {word} in a sentence.", "Вона використала слово «{translation}» у реченні."),
    ("He checked the meaning of the word {word}.", "Він перевірив значення слова «{translation}»."),
    ("Please repeat the word {word} aloud.", "Повтори слово «{translation}» уголос, будь ласка."),
    ("We added the word {word} to our notes.", "Ми додали слово «{translation}» до нотаток."),
    ("Do you remember the word {word} from yesterday?", "Ти пам'ятаєш слово «{translation}» із вчорашнього уроку?"),
)
_VERB_TEMPLATES = (
    ("I want to {word} today.", "Сьогодні я хочу {translation}."),
    ("Can you {word} after class?", "Можеш {translation} після заняття?"),
    ("She plans to {word} this week.", "Вона планує {translation} цього тижня."),
    ("We need to {word} before lunch.", "Нам потрібно {translation} перед обідом."),
    ("They decided to {word} together.", "Вони вирішили {translation} разом."),
    ("He likes to {word} in the morning.", "Він любить {translation} вранці."),
    ("You can {word} after the meeting.", "Ти можеш {translation} після зустрічі."),
    ("I learned to {word} at school.", "У школі я навчився {translation}."),
    ("We are ready to {word} now.", "Ми готові {translation} зараз."),
    ("She wants to {word} on her own.", "Вона хоче {translation} самостійно."),
)
_BUSINESS_ADJECTIVE_TEMPLATES = (
    ("The word {word} describes this process.", "Слово «{translation}» описує цей процес."),
    ("Our team calls this option {word}.", "Наша команда називає цей варіант словом «{translation}»."),
    ("We described the service as {word}.", "Ми описали послугу словом «{translation}»."),
    ("Could {word} describe this result?", "Чи може слово «{translation}» описати цей результат?"),
    ("Use {word} in the project summary.", "Використай слово «{translation}» у підсумку проєкту."),
)
_GENERAL_ADJECTIVE_EXAMPLES = {
    "happy": ("She feels happy today.", "Щаслива людина сьогодні усміхається."),
    "sad": ("He looks sad today.", "Сумна людина сьогодні потребує підтримки."),
    "tired": ("I feel tired after work.", "Втомлена людина сьогодні відпочиває."),
    "busy": ("My friend is busy at work today.", "Зайнята людина сьогодні працює."),
    "careful": ("Be careful when you check the details.", "Обережна людина перевіряє деталі."),
    "calm": ("She stays calm and speaks quietly.", "Спокійна людина говорить тихо."),
    "confident": ("He feels confident when he speaks.", "Упевнена людина говорить чітко."),
    "excited": ("They are excited about the event.", "Схвильована людина чекає на подію."),
    "nervous": ("I feel nervous before a meeting.", "Нервова людина готується до зустрічі."),
    "ready": ("Are you ready to start?", "Готова людина може починати."),
    "safe": ("The child is safe at home.", "Ця людина зараз у безпеці."),
    "comfortable": ("I feel comfortable in this chair.", "Ця людина почувається комфортно."),
    "healthy": ("A healthy person has more energy.", "Здорова людина має більше сил."),
    "different": ("This person is different from the others.", "Ця людина не схожа на інших."),
    "friendly": ("Our neighbor is friendly and happy to help.", "Привітна людина радо допомагає."),
    "kind": ("A kind person helps other people.", "Добра людина допомагає іншим."),
    "polite": ("Be polite when you say hello.", "Ввічлива людина чемно вітається."),
    "quiet": ("The quiet student listens carefully.", "Тиха людина уважно слухає."),
    "loud": ("The music is loud in this room.", "У цій кімнаті гучно грає музика."),
    "strong": ("She is strong enough to carry the suitcase.", "Сильна людина може нести валізу."),
}


def _placement_questions() -> list[dict[str, Any]]:
    questions = [
        ("A1", "grammar", "I ___ from Ukraine.", ["am", "is", "are", "be"], 0),
        ("A1", "vocabulary", "What is the opposite of “big”?", ["small", "long", "old", "fast"], 0),
        ("A1", "reading", "“Mia is 10. She has a cat.” How old is Mia?", ["Ten", "Eight", "Twelve", "We don't know"], 0),
        ("A1", "usage", "Choose a polite greeting for 9 a.m.", ["Good morning", "Good night", "Good evening", "Goodbye"], 0),
        ("A2", "grammar", "Yesterday, we ___ a new café.", ["visit", "visited", "visiting", "have visit"], 1),
        ("A2", "vocabulary", "There isn't ___ milk in the fridge.", ["many", "some", "any", "few"], 2),
        ("A2", "reading", "“Tom usually walks, but today he is taking the bus.” How is Tom travelling today?", ["By bus", "On foot", "By train", "By bike"], 0),
        ("A2", "usage", "This bag is ___ than that one.", ["heavy", "heavier", "heaviest", "more heavy"], 1),
        ("B1", "grammar", "If it rains tomorrow, we ___ at home.", ["stay", "will stay", "stayed", "would stayed"], 1),
        ("B1", "vocabulary", "I have lived here ___ 2020.", ["for", "since", "during", "from"], 1),
        ("B1", "reading", "“Although the train was delayed, Ella arrived before the meeting.” Was Ella late for the meeting?", ["No", "Yes", "The meeting was cancelled", "We don't know"], 0),
        ("B1", "usage", "He suggested ___ the earlier train.", ["take", "to take", "taking", "taken"], 2),
        ("B2", "grammar", "The new library ___ next spring.", ["will open", "will be opened", "has opening", "is open"], 1),
        ("B2", "vocabulary", "The report provides a ___ overview of the risks.", ["comprehensive", "comprehensible", "comprehended", "comprehending"], 0),
        ("B2", "reading", "“The policy reduced costs, but its effect on staff morale remains unclear.” What is uncertain?", ["The effect on staff morale", "Whether costs fell", "Who wrote the policy", "When costs were measured"], 0),
        ("B2", "usage", "If I ___ about the change, I would have called you.", ["knew", "had known", "would know", "have known"], 1),
        ("C1", "grammar", "Rarely ___ such a compelling argument.", ["I have heard", "have I heard", "I heard", "did I have heard"], 1),
        ("C1", "vocabulary", "The findings are ___ with the earlier study.", ["consistent", "constant", "consequent", "conscious"], 0),
        ("C1", "reading", "“The proposal is feasible, provided that adequate funding is secured.” What condition must be met?", ["Enough funding must be secured", "The proposal must be shortened", "The findings must be published", "Costs must increase"], 0),
        ("C1", "usage", "The chair asked us to ___ the discussion until Friday.", ["defer", "infer", "confer", "refer"], 0),
        ("C2", "grammar", "Not until the data had been reanalysed ___ the error.", ["the team noticed", "did the team notice", "the team had noticed", "noticed the team"], 1),
        ("C2", "vocabulary", "Her account was so ___ that it left no room for doubt.", ["unequivocal", "ambiguous", "tentative", "incidental"], 0),
        ("C2", "reading", "“Far from being an isolated case, the incident exposed a recurring weakness.” What does the sentence imply?", ["The weakness had appeared before", "The incident was unimportant", "The weakness was immediately fixed", "No similar cases existed"], 0),
        ("C2", "usage", "Choose the sentence with the most suitable formal register.", ["The results are inconclusive; further evidence is required.", "The results are kinda unclear, so we need more stuff.", "The results are bad and we need to do things.", "The results are a bit weird, let's get more."], 0),
    ]
    result: list[dict[str, Any]] = []
    for index, (level, skill, prompt, source_options, source_correct_index) in enumerate(
        questions, start=1
    ):
        question_id = f"placement-{level.lower()}-{skill}-{index}"
        options = list(source_options)
        correct_answer = options[source_correct_index]
        random.Random(question_id).shuffle(options)
        result.append({
            "id": question_id,
            "level": level,
            "skill": skill,
            "prompt": prompt,
            "options": options,
            "correct_index": options.index(correct_answer),
        })
    return result


def build_course_catalog(
    courses: list[dict[str, Any]], word_bank: dict[str, Any]
) -> list[dict[str, Any]]:
    result = [dict(course) for course in courses]
    track_courses = {course["track"]: course for course in result}
    for track in ("general", "business"):
        if track not in track_courses:
            raise ValueError(f"The course for track {track!r} is missing.")
        course = track_courses[track]
        course["placement_test"] = _placement_questions()
        existing_words = {
            word["word"].casefold()
            for lesson in course["lessons"]
            for word in lesson["vocabulary"]
        }
        entries: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for category_index, category in enumerate(word_bank.get(track, []), start=1):
            for word, translation, part, level in _category_entries(category):
                normalized = word.casefold()
                if normalized in seen:
                    raise ValueError(f"Duplicate vocabulary word {word!r} in the {track} course.")
                seen.add(normalized)
                if normalized in existing_words:
                    continue
                entries.append((word, translation, part, level, category["title"]))
        if not entries and not course["lessons"]:
            raise ValueError(f"The vocabulary bank for track {track!r} is empty.")

        order = max((lesson["order"] for lesson in course["lessons"]), default=0) + 1
        words_per_lesson = 5
        categories = word_bank[track]
        for category_index, category in enumerate(categories, start=1):
            category_entries = [
                entry for entry in entries if entry[4] == category["title"]
            ]
            for offset in range(0, len(category_entries), words_per_lesson):
                group = category_entries[offset : offset + words_per_lesson]
                part_index = offset // words_per_lesson + 1
                lesson_id = f"{catalog_prefix}{category_index}-{part_index}"
                vocabulary: list[dict[str, str]] = []
                exercises: list[dict[str, Any]] = []
                for group_index, (word, translation, part, level, _) in enumerate(group):
                    word_id = f"catalog-{track}-{_slug_word(word)}"
                    example, example_translation = _example(
                        word, translation, part, track, offset + group_index
                    )
                    vocabulary.append(
                        {
                            "id": word_id,
                            "word": word,
                            "translation": translation,
                            "part_of_speech": _UK_PARTS[part],
                            "example": example,
                            "example_translation": example_translation,
                        }
                    )
                    for exercise_round in range(2):
                        exercise = _catalog_exercise(
                            word=word,
                            translation=translation,
                            part=part,
                            level=level,
                            entries=entries,
                            category_entries=category_entries,
                            track=track,
                            word_id=word_id,
                            example=example,
                            example_translation=example_translation,
                            context_offset=offset,
                            exercise_kind=(group_index + exercise_round) % 5,
                        )
                        exercise["id"] = f"exercise-{word_id}-{exercise_round + 1}"
                        exercises.append(exercise)
                course["lessons"].append(
                    {
                        "id": lesson_id,
                        "order": order,
                        "level": category["level"],
                        "title": f"{category['title']} — частина {part_index}",
                        "objectives": (
                            f"Вивчити слова з теми «{category['title']}» "
                            "та потренувати їх у реченнях."
                        ),
                        "explanation": (
                            "Прочитайте слова з перекладом і прикладами, "
                            "а потім виконайте завдання на контекст, переклад слова чи речення "
                            "або введіть слово самостійно."
                        ),
                        "vocabulary": vocabulary,
                        "exercises": exercises,
                    }
                )
                order += 1
        course["lessons"].sort(key=lambda lesson: lesson["order"])
    return result


def _category_entries(
    category: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for row in category.get("words", []):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"Each word in {category.get('title', 'course content')!r} needs word, translation, and part of speech.")
        word, translation, part = row
        if part not in _UK_PARTS:
            raise ValueError(f"Unsupported part of speech: {part!r}.")
        if not all(isinstance(value, str) and value.strip() for value in (word, translation)):
            raise ValueError("Vocabulary words and translations must be non-empty text.")
        entries.append((word, translation, part, category["level"]))
    if not category.get("title") or category.get("level") not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
        raise ValueError("Each vocabulary topic needs a title and valid CEFR level.")
    return entries


def _example(
    word: str, translation: str, part: str, track: str, context_index: int = 0
) -> tuple[str, str]:
    if part == "noun":
        if track == "general" and word.casefold() in _GENERAL_NOUN_EXAMPLES:
            example, translated = _GENERAL_NOUN_EXAMPLES[word.casefold()]
            return example.replace("____", word, 1), translated
        template, translated = _GENERAL_NOUN_TEMPLATES[context_index % len(_GENERAL_NOUN_TEMPLATES)]
        return template.format(word=word), translated.format(translation=translation)
    if part == "verb":
        template, translated = _VERB_TEMPLATES[context_index % len(_VERB_TEMPLATES)]
        return template.format(word=word), translated.format(translation=translation)
    if track == "general":
        example = _GENERAL_ADJECTIVE_EXAMPLES.get(word)
        if not example:
            raise ValueError(f"Add an example sentence for the general adjective {word!r}.")
        return example
    template, translated = _BUSINESS_ADJECTIVE_TEMPLATES[
        context_index % len(_BUSINESS_ADJECTIVE_TEMPLATES)
    ]
    return template.format(word=word), translated.format(translation=translation)


def _catalog_exercise(
    *,
    word: str,
    translation: str,
    part: str,
    level: str,
    entries: list[tuple[str, str, str, str, str]],
    category_entries: list[tuple[str, str, str, str, str]],
    track: str,
    word_id: str,
    example: str,
    example_translation: str,
    context_offset: int,
    exercise_kind: int,
) -> dict[str, Any]:
    answer_mode = "choice"
    if exercise_kind == 0:
        prompt = example.replace(word, "____", 1)
        correct_answer = word
        options = _exercise_options(word, part, level, entries, word_id)
        explanation = f"«{word}» означає «{translation}»."
    elif exercise_kind == 1:
        prompt = f"Оберіть український переклад слова “{word}”:"
        correct_answer = translation
        options = _choice_options(
            translation,
            [
                candidate_translation
                for _, candidate_translation, candidate_part, candidate_level, _
                in entries
                if candidate_part == part and candidate_level == level
            ],
            word_id,
        )
        explanation = f"«{word}» українською — «{translation}»."
    elif exercise_kind == 2:
        prompt = f"Оберіть англійський відповідник до «{translation}»:"
        correct_answer = word
        options = _exercise_options(word, part, level, entries, word_id)
        explanation = f"«{translation}» англійською — “{word}”."
    elif exercise_kind == 3:
        prompt = f"Оберіть правильний переклад речення:\n{example}"
        correct_answer = example_translation
        sentence_translations = [
            _example(
                candidate_word,
                candidate_translation,
                candidate_part,
                track,
                context_offset + candidate_index,
            )[1]
            for candidate_index, (
                candidate_word,
                candidate_translation,
                candidate_part,
                _,
                _,
            ) in enumerate(category_entries)
        ]
        options = _choice_options(example_translation, sentence_translations, word_id)
        explanation = f"Переклад: «{example_translation}»."
    else:
        prompt = f"Напишіть англійське слово для перекладу «{translation}»:"
        correct_answer = word
        options = []
        answer_mode = "text"
        explanation = f"«{translation}» англійською — “{word}”."
    return {
        "sentence": prompt,
        "options": options,
        "correct_answer": correct_answer,
        "explanation": explanation,
        "max_attempts": 2,
        "answer_mode": answer_mode,
    }


def _exercise_options(
    correct: str,
    part: str,
    level: str,
    entries: list[tuple[str, str, str, str, str]],
    seed: str,
) -> list[str]:
    candidates = [
        word
        for word, _, candidate_part, candidate_level, _ in entries
        if candidate_part == part and candidate_level == level and word != correct
    ]
    if len(candidates) < 2:
        candidates = [
            word
            for word, _, candidate_part, _, _ in entries
            if candidate_part == part and word != correct
        ]
    if len(candidates) < 2:
        raise ValueError(f"Not enough vocabulary distractors for {correct!r}.")
    return _choice_options(correct, candidates, seed)


def _choice_options(correct: str, candidates: list[str], seed: str) -> list[str]:
    distractors = list(dict.fromkeys(candidate for candidate in candidates if candidate != correct))
    if len(distractors) < 2:
        raise ValueError(f"Not enough distinct distractors for {correct!r}.")
    randomizer = random.Random(seed)
    randomizer.shuffle(distractors)
    options = [correct, *distractors[:2]]
    randomizer.shuffle(options)
    return options


def _slug_word(word: str) -> str:
    return _SLUG.sub("-", word.casefold()).strip("-")
