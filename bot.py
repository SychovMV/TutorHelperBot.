import asyncio
import io
import json
import logging
import os
import re
import tempfile
from uuid import uuid4

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from openai import AsyncOpenAI


load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
router = Router()

QUIZ_SESSIONS: dict[int, dict] = {}
ORAL_SESSIONS: dict[int, dict] = {}
USER_LAST_LESSON_TEXT: dict[int, str] = {}
USER_PENDING_FILES: dict[int, dict] = {}

START_TEXT = (
    "Здравствуйте. Я помогу закрепить материал урока. "
    "Пришлите объяснение нового материала в аудио формате или формате TXT \n\n"
    "Пришлите файл в качестве ответа на это сообщение"
)

AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a",
    ".wav", ".webm", ".ogg", ".oga", ".flac",
}


def mode_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Тест", callback_data=f"mode_test:{user_id}")],
            [InlineKeyboardButton(text="🎙 Вопрос-ответ", callback_data=f"mode_oral:{user_id}")],
        ]
    )


def finish_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Пройти тест снова", callback_data=f"restart_quiz:{user_id}")],
            [InlineKeyboardButton(text="📚 Закрепить материал другого урока", callback_data=f"new_lesson:{user_id}")],
        ]
    )


async def get_text_from_pending_file(user_id: int) -> str | None:
    file_data = USER_PENDING_FILES.get(user_id)

    if not file_data:
        return None

    file_name = file_data["file_name"]
    raw_data = file_data["raw_data"]

    if file_name.endswith(".txt"):
        try:
            return raw_data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return raw_data.decode("cp1251").strip()

    return await transcribe_audio(raw_data, file_name)


async def generate_quiz_json(text: str) -> list[dict]:
    prompt = f"""
Составь 10 интерактивных заданий по материалу урока.

Верни ТОЛЬКО валидный JSON-массив без markdown, без ```json и без пояснений.

Формат:
{{
  "number": 1,
  "type": "single_choice",
  "question": "Текст вопроса",
  "options": ["А. ...", "Б. ...", "В. ...", "Г. ..."],
  "correct": ["А. ..."],
  "explanation": "Короткое объяснение"
}}

Типы заданий:
1-3: single_choice.
- options: ровно 4 варианта.
- correct: ровно 1 вариант, полностью совпадающий с options.

4-5: multiple_choice.
- options: ровно 5 вариантов.
- correct: 2 или 3 варианта, полностью совпадающие с options.

6: matching_2.
- options: [].
- correct: одна строка вида ["1а 2в 3б 4г"].
- В question сделай списки друг под другом.
- Первый список: 1, 2, 3, 4.
- Второй список: А, Б, В, Г.
- Второй список начинай после абзацного отступа: \\n\\n    Список 2:
- Инструкцию формата ответа не добавляй.

7: matching_3_4.
- options: [].
- correct: одна строка вида ["1аI 2бII 3вIII 4гIV"].
- Все списки должны идти друг под другом.
- Инструкцию формата ответа не добавляй.

8: ordering.
- ОБЯЗАТЕЛЬНО сделай options.
- options должен содержать ровно 4 готовых варианта последовательности.
- Каждый option должен быть полной последовательностью.
- correct должен содержать ровно 1 вариант, полностью совпадающий с одним из options.
- НЕЛЬЗЯ оставлять options пустым.

9: find_errors.
- options должен содержать 4-5 фрагментов текста.
- correct должен содержать ошибочные фрагменты из options.
- НЕЛЬЗЯ оставлять options пустым.

10: short_answer.
- options: [].
- correct: один ответ из 1-2 слов.

Материал:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты создаёшь интерактивные учебные задания в строгом JSON-формате."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=3500,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def repair_button_question(question: dict, lesson_text: str) -> dict:
    repair_prompt = f"""
Исправь задание так, чтобы оно подходило для кнопок в Telegram.

Верни ТОЛЬКО JSON-объект без markdown.

Старое задание:
{json.dumps(question, ensure_ascii=False)}

Требования:
- type оставь тем же.
- Для single_choice нужно ровно 4 options и 1 correct.
- Для multiple_choice нужно ровно 5 options и 2-3 correct.
- Для ordering нужно ровно 4 options, каждый option — полная последовательность; correct — 1 правильная последовательность из options.
- Для find_errors нужно 4-5 options; correct — ошибочные фрагменты из options.
- Все correct должны полностью совпадать с элементами options.
- options нельзя оставлять пустым.
- Пиши на русском языке.
- Не выходи за рамки материала.

Материал:
{lesson_text[:6000]}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты исправляешь тестовое задание в строгом JSON-формате."},
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.2,
        max_tokens=1200,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def generate_oral_question(lesson_text: str, history: list[dict]) -> dict:
    prompt = f"""
Ты — строгий, но доброжелательный экзаменатор.

Веди с учеником живой устный опрос по материалу урока.
Следующий вопрос должен зависеть от материала и предыдущих ответов ученика.

Верни ТОЛЬКО JSON-объект:
{{
  "question": "Следующий вопрос ученику",
  "reason": "Почему ты задаёшь именно этот вопрос"
}}

Правила:
- Задай только один вопрос.
- Не давай ответ.
- Не составляй список вопросов заранее.
- Если ученик ответил слабо, задай более простой или уточняющий вопрос.
- Если ученик ответил хорошо, задай более глубокий вопрос.
- Не задавай вопросы, на которые можно ответить только «да» или «нет».

Материал урока:
{lesson_text[:12000]}

История диалога:
{json.dumps(history, ensure_ascii=False)}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты проводишь устный опрос по учебному материалу."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=700,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def evaluate_oral_answer(lesson_text: str, question: str, answer: str) -> dict:
    prompt = f"""
Оцени ответ ученика на устный вопрос по материалу урока.

Верни ТОЛЬКО JSON-объект:
{{
  "score": 0,
  "feedback": "Развёрнутый комментарий ученику",
  "correct_answer": "Как можно было ответить лучше",
  "what_to_ask_next": "Что стоит проверить следующим вопросом"
}}

score:
0 — неверно
1 — частично верно
2 — верно

Материал урока:
{lesson_text[:12000]}

Вопрос:
{question}

Ответ ученика:
{answer}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты проверяешь устный ответ ученика."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=900,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


def count_words(text: str) -> int:
    return len(re.findall(r"[А-Яа-яA-Za-zЁё0-9]+", text))


async def repair_short_answer_question(question: dict, lesson_text: str) -> dict:
    repair_prompt = f"""
Переделай задание short_answer так, чтобы правильный ответ состоял строго из 1 или 2 слов.

Верни ТОЛЬКО JSON-объект.

Формат:
{{
  "number": 10,
  "type": "short_answer",
  "question": "Вопрос, который требует ответа 1-2 словами",
  "options": [],
  "correct": ["ответ"],
  "explanation": "Короткое объяснение"
}}

Старое задание:
{json.dumps(question, ensure_ascii=False)}

Материал:
{lesson_text[:6000]}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты исправляешь учебное задание в JSON."},
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.2,
        max_tokens=900,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def repair_invalid_questions(quiz: list[dict], lesson_text: str) -> list[dict]:
    repaired = []

    button_types = {"single_choice", "multiple_choice", "ordering", "find_errors"}

    for question in quiz:
        if not isinstance(question, dict):
            continue

        question_type = question.get("type", "")
        options = question.get("options", [])
        correct = question.get("correct", [])

        if question_type == "short_answer":
            if not isinstance(correct, list):
                correct = [str(correct)]

            correct = [str(item).strip() for item in correct if str(item).strip()]

            if len(correct) != 1 or count_words(correct[0]) < 1 or count_words(correct[0]) > 2:
                try:
                    question = await repair_short_answer_question(question, lesson_text)
                except Exception:
                    question = {
                        "number": question.get("number", 10),
                        "type": "short_answer",
                        "question": "Как называется человек, находящийся в собственности другого человека?",
                        "options": [],
                        "correct": ["раб"],
                        "explanation": "Раб — это человек, лишённый свободы и находящийся в собственности другого человека.",
                    }

        elif question_type in button_types:
            need_repair = False

            if not isinstance(options, list) or len(options) == 0:
                need_repair = True

            if not isinstance(correct, list) or len(correct) == 0:
                need_repair = True

            if isinstance(options, list) and isinstance(correct, list):
                for answer in correct:
                    if answer not in options:
                        need_repair = True

            if question_type == "ordering" and isinstance(options, list) and len(options) < 2:
                need_repair = True

            if need_repair:
                try:
                    question = await repair_button_question(question, lesson_text)
                except Exception:
                    logging.exception("Button question repair error")
                    question = {
                        "number": question.get("number", 8),
                        "type": "single_choice",
                        "question": "Какое утверждение лучше всего соответствует материалу урока?",
                        "options": [
                            "А. Основная мысль изложена в материале урока",
                            "Б. Это утверждение не связано с материалом",
                            "В. Это противоположно материалу",
                            "Г. Это случайный факт",
                        ],
                        "correct": ["А. Основная мысль изложена в материале урока"],
                        "explanation": "Вопрос был автоматически исправлен, потому что модель вернула пустые варианты ответа.",
                    }

        repaired.append(question)

    return repaired


def normalize_question(question: dict, fallback_number: int) -> dict:
    question["number"] = question.get("number", fallback_number)
    question["type"] = question.get("type", "single_choice")
    question["question"] = question.get("question", "Вопрос")
    question["options"] = question.get("options", [])
    question["correct"] = question.get("correct", [])
    question["explanation"] = question.get("explanation", "Объяснение не указано.")

    if not isinstance(question["options"], list):
        question["options"] = []

    if not isinstance(question["correct"], list):
        question["correct"] = [str(question["correct"])]

    question["options"] = [str(option).strip() for option in question["options"] if str(option).strip()]
    question["correct"] = [str(answer).strip() for answer in question["correct"] if str(answer).strip()]

    question_type = question["type"]

    if question_type in {"matching_2", "matching_3_4"}:
        question["options"] = []
        question["correct"] = question["correct"][:1]
        return question

    if question_type == "short_answer":
        question["options"] = []
        question["correct"] = question["correct"][:1]
        return question

    valid_correct = [answer for answer in question["correct"] if answer in question["options"]]

    if valid_correct:
        question["correct"] = valid_correct
    elif question["options"]:
        question["correct"] = [question["options"][0]]

    if question_type == "single_choice":
        question["options"] = question["options"][:4]
        question["correct"] = question["correct"][:1]

    if question_type == "ordering":
        question["correct"] = question["correct"][:1]

    return question


def normalize_quiz(quiz: list[dict]) -> list[dict]:
    return [
        normalize_question(question, index)
        for index, question in enumerate(quiz[:10], start=1)
        if isinstance(question, dict)
    ]


async def transcribe_audio(file_bytes: bytes, file_name: str) -> str:
    _, extension = os.path.splitext(file_name.lower())

    if extension not in AUDIO_EXTENSIONS:
        extension = ".ogg"

    with tempfile.NamedTemporaryFile(suffix=extension, delete=True) as temp_audio:
        temp_audio.write(file_bytes)
        temp_audio.flush()

        with open(temp_audio.name, "rb") as audio_file:
            transcription = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )

    return transcription.text.strip()


def normalize_answer(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def normalize_matching_answer(text: str) -> str:
    text = normalize_answer(text)
    text = text.replace("-", "").replace("—", "").replace("–", "")
    text = text.replace(",", " ").replace(";", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(sorted(text.split()))


def answer_result(question_type: str, user_answer, correct: list[str]) -> dict:
    if question_type in {"multiple_choice", "find_errors"}:
        user_set = set(user_answer)
        correct_set = set(correct)

        if user_set == correct_set:
            return {"status": "correct", "points": 1.0}

        if user_set & correct_set:
            return {"status": "partial", "points": 0.5}

        return {"status": "wrong", "points": 0.0}

    if question_type in {"matching_2", "matching_3_4"}:
        user_parts = set(normalize_matching_answer(str(user_answer)).split())
        correct_parts = set(normalize_matching_answer(str(correct[0] if correct else "")).split())

        if user_parts == correct_parts:
            return {"status": "correct", "points": 1.0}

        if user_parts & correct_parts:
            return {"status": "partial", "points": 0.5}

        return {"status": "wrong", "points": 0.0}

    if question_type == "short_answer":
        normalized_user = normalize_answer(str(user_answer))
        normalized_correct = [normalize_answer(str(answer)) for answer in correct]

        if normalized_user in normalized_correct:
            return {"status": "correct", "points": 1.0}

        return {"status": "wrong", "points": 0.0}

    if user_answer in correct:
        return {"status": "correct", "points": 1.0}

    return {"status": "wrong", "points": 0.0}


def get_user_id_from_message(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def make_keyboard(question: dict, session_id: str) -> InlineKeyboardMarkup:
    question_type = question.get("type", "")
    options = question.get("options", [])
    buttons = []

    if question_type in {"multiple_choice", "find_errors"}:
        for index, option in enumerate(options):
            buttons.append([InlineKeyboardButton(text=option[:60], callback_data=f"toggle:{session_id}:{index}")])

        buttons.append([InlineKeyboardButton(text="✅ Ответить", callback_data=f"submit:{session_id}")])
    else:
        for index, option in enumerate(options):
            buttons.append([InlineKeyboardButton(text=option[:60], callback_data=f"single:{session_id}:{index}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_current_question(message_or_callback, user_id: int) -> None:
    session = QUIZ_SESSIONS.get(user_id)

    if not session:
        return

    index = session["current_index"]
    quiz = session["quiz"]

    if index >= len(quiz):
        await finish_quiz(message_or_callback, user_id)
        return

    question = quiz[index]
    session["selected"] = set()

    number = question.get("number", index + 1)
    question_text = question.get("question", "")
    question_type = question.get("type", "")
    options = question.get("options", [])

    text = f"<b>Вопрос {number} из {len(quiz)}</b>\n\n{question_text}"

    if question_type in {"short_answer", "matching_2", "matching_3_4"}:
        session["awaiting_text_answer"] = True

        if question_type == "short_answer":
            text += "\n\nНапишите ответ одним сообщением. Ответ должен состоять из 1-2 слов."
        elif question_type == "matching_2":
            text += "\n\nВведите ответ в формате: 1а 2б 3в 4г"
        elif question_type == "matching_3_4":
            text += "\n\nВведите ответ в формате: 1аI 2бII 3вIII 4гIV"

        await message_or_callback.answer(text)
        return

    session["awaiting_text_answer"] = False

    if not options:
        await message_or_callback.answer(
            text + "\n\nУ этого вопроса не были созданы варианты ответа. Перехожу к следующему вопросу."
        )
        session["answers"].append(
            {
                "question_number": number,
                "status": "wrong",
                "points": 0.0,
            }
        )
        session["current_index"] += 1
        await send_current_question(message_or_callback, user_id)
        return

    await message_or_callback.answer(
        text,
        reply_markup=make_keyboard(question, session["session_id"]),
    )


async def show_answer_and_next(message: Message, user_id: int, user_answer, result: dict) -> None:
    session = QUIZ_SESSIONS.get(user_id)

    if not session:
        return

    question = session["quiz"][session["current_index"]]
    correct = question.get("correct", [])
    explanation = question.get("explanation", "")

    status = result["status"]
    points = result["points"]

    if status == "correct":
        result_text = "✅ Верно!"
    elif status == "partial":
        result_text = "🟡 Частично верно."
    else:
        result_text = "❌ Неверно."

    if isinstance(user_answer, list):
        user_answer_text = "\n".join(user_answer) if user_answer else "Ответ не выбран"
    else:
        user_answer_text = str(user_answer)

    correct_text = "\n".join(correct)

    await message.answer(
        f"{result_text}\n\n"
        f"<b>Ваш ответ:</b>\n{user_answer_text}\n\n"
        f"<b>Правильный ответ:</b>\n{correct_text}\n\n"
        f"<b>Баллы за вопрос:</b> {points}/1\n\n"
        f"<b>Объяснение:</b>\n{explanation}"
    )

    session["answers"].append(
        {
            "question_number": question.get("number", session["current_index"] + 1),
            "status": status,
            "points": points,
        }
    )

    session["score"] += points
    session["current_index"] += 1
    session["awaiting_text_answer"] = False
    session["selected"] = set()

    await asyncio.sleep(0.8)

    if session["current_index"] >= len(session["quiz"]):
        await finish_quiz(message, user_id)
    else:
        await send_current_question(message, user_id)


async def finish_quiz(message_or_callback, user_id: int) -> None:
    session = QUIZ_SESSIONS.get(user_id)

    if not session:
        return

    score = session["score"]
    total = len(session["quiz"])
    percent = round(score / total * 100) if total else 0

    correct_count = sum(1 for item in session["answers"] if item["status"] == "correct")
    partial_count = sum(1 for item in session["answers"] if item["status"] == "partial")
    wrong_count = sum(1 for item in session["answers"] if item["status"] == "wrong")

    await message_or_callback.answer(
        f"🏁 <b>Тест завершён!</b>\n\n"
        f"Баллы: <b>{score} из {total}</b>\n"
        f"Результат: <b>{percent}%</b>\n\n"
        f"✅ Верно: {correct_count}\n"
        f"🟡 Частично верно: {partial_count}\n"
        f"❌ Неверно: {wrong_count}\n\n"
        f"Что сделать дальше?",
        reply_markup=finish_keyboard(user_id),
    )

    QUIZ_SESSIONS.pop(user_id, None)


async def start_quiz_from_text(message: Message, text: str, user_id: int) -> None:
    if len(text) > 14000:
        text = text[:14000]

    USER_LAST_LESSON_TEXT[user_id] = text

    await message.answer("Готовлю тест...")

    try:
        quiz = await generate_quiz_json(text)
        quiz = await repair_invalid_questions(quiz, text)
        quiz = normalize_quiz(quiz)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    QUIZ_SESSIONS[user_id] = {
        "session_id": str(uuid4()),
        "quiz": quiz,
        "current_index": 0,
        "score": 0.0,
        "answers": [],
        "selected": set(),
        "awaiting_text_answer": False,
    }

    await message.answer("Тест готов. Начинаем!")
    await send_current_question(message, user_id)


async def start_oral_from_text(message: Message, text: str, user_id: int) -> None:
    if len(text) > 14000:
        text = text[:14000]

    USER_LAST_LESSON_TEXT[user_id] = text

    ORAL_SESSIONS[user_id] = {
        "lesson_text": text,
        "history": [],
        "current_question": None,
        "question_count": 0,
        "score": 0,
        "max_questions": 10,
    }

    await message.answer(
        "Начинаем режим вопрос-ответ.\n\n"
        "Я буду задавать вопросы по одному, как на устном экзамене. "
        "Следующий вопрос будет зависеть от вашего предыдущего ответа."
    )

    await send_next_oral_question(message, user_id)


async def send_next_oral_question(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    if not session:
        return

    if session["question_count"] >= session["max_questions"]:
        await finish_oral(message, user_id)
        return

    try:
        question_data = await generate_oral_question(
            lesson_text=session["lesson_text"],
            history=session["history"],
        )
    except Exception as error:
        logging.exception("Oral question generation error")
        await message.answer(f"Ошибка при генерации вопроса:\n{error}")
        return

    question = question_data.get("question", "Расскажите главное по теме.")

    session["current_question"] = question
    session["question_count"] += 1

    await message.answer(
        f"<b>Вопрос {session['question_count']}</b>\n\n"
        f"{question}\n\n"
        "Ответьте одним сообщением."
    )


async def process_oral_answer(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    if not session:
        await message.answer("Сессия вопрос-ответ не найдена. Пришлите файл заново.")
        return

    user_answer = message.text.strip()
    current_question = session.get("current_question")

    if not current_question:
        await send_next_oral_question(message, user_id)
        return

    try:
        evaluation = await evaluate_oral_answer(
            lesson_text=session["lesson_text"],
            question=current_question,
            answer=user_answer,
        )
    except Exception as error:
        logging.exception("Oral evaluation error")
        await message.answer(f"Ошибка при проверке ответа:\n{error}")
        return

    score = int(evaluation.get("score", 0))
    feedback = evaluation.get("feedback", "")
    correct_answer = evaluation.get("correct_answer", "")
    what_to_ask_next = evaluation.get("what_to_ask_next", "")

    session["score"] += score

    session["history"].append(
        {
            "question": current_question,
            "answer": user_answer,
            "score": score,
            "feedback": feedback,
            "correct_answer": correct_answer,
            "what_to_ask_next": what_to_ask_next,
        }
    )

    await message.answer(
        f"<b>Оценка:</b> {score}/2\n\n"
        f"<b>Комментарий:</b>\n{feedback}\n\n"
        f"<b>Как можно было ответить лучше:</b>\n{correct_answer}"
    )

    await asyncio.sleep(0.8)
    await send_next_oral_question(message, user_id)


async def finish_oral(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    if not session:
        return

    score = session["score"]
    total = session["question_count"] * 2
    percent = round(score / total * 100) if total else 0

    lines = [
        "🏁 <b>Устный опрос завершён!</b>",
        "",
        f"Результат: <b>{score} из {total}</b>",
        f"Процент: <b>{percent}%</b>",
        "",
        "<b>Краткая статистика:</b>",
    ]

    for index, item in enumerate(session["history"], start=1):
        mark = "✅" if item["score"] == 2 else "🟡" if item["score"] == 1 else "❌"
        lines.append(f"{mark} Вопрос {index}: {item['score']}/2")

    await message.answer("\n".join(lines))
    ORAL_SESSIONS.pop(user_id, None)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if user_id:
        QUIZ_SESSIONS.pop(user_id, None)
        ORAL_SESSIONS.pop(user_id, None)
        USER_PENDING_FILES.pop(user_id, None)

    await message.answer(START_TEXT)


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    document = message.document

    if not document.file_name:
        await message.answer("Не удалось определить имя файла.")
        return

    file_name = document.file_name.lower()

    if not file_name.endswith(".txt") and not any(file_name.endswith(ext) for ext in AUDIO_EXTENSIONS):
        await message.answer("Пришлите файл в формате TXT или аудио.")
        return

    downloaded_file = await bot.download(document)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать файл.")
        return

    USER_PENDING_FILES[user_id] = {
        "file_name": file_name,
        "raw_data": downloaded_file.getvalue(),
    }

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await message.answer(
        "Файл получен. Выберите режим работы:",
        reply_markup=mode_keyboard(user_id),
    )


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    downloaded_file = await bot.download(message.voice)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать голосовое сообщение.")
        return

    USER_PENDING_FILES[user_id] = {
        "file_name": "voice.ogg",
        "raw_data": downloaded_file.getvalue(),
    }

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await message.answer(
        "Голосовое сообщение получено. Выберите режим работы:",
        reply_markup=mode_keyboard(user_id),
    )


@router.message(F.audio)
async def audio_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    downloaded_file = await bot.download(message.audio)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать аудио.")
        return

    USER_PENDING_FILES[user_id] = {
        "file_name": message.audio.file_name or "audio.mp3",
        "raw_data": downloaded_file.getvalue(),
    }

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await message.answer(
        "Аудио получено. Выберите режим работы:",
        reply_markup=mode_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("restart_quiz:"))
async def restart_quiz_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    lesson_text = USER_LAST_LESSON_TEXT.get(user_id)

    if not lesson_text:
        await callback.answer()
        await callback.message.answer("Не нашёл предыдущий материал. Пришлите файл заново.")
        return

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await callback.answer()
    await callback.message.answer("Создаю новый тест по тому же материалу...")

    await start_quiz_from_text(callback.message, lesson_text, user_id)


@router.callback_query(F.data.startswith("new_lesson:"))
async def new_lesson_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)
    USER_PENDING_FILES.pop(user_id, None)
    USER_LAST_LESSON_TEXT.pop(user_id, None)

    await callback.answer()
    await callback.message.answer(START_TEXT)


@router.callback_query(F.data.startswith("mode_test:"))
async def mode_test_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    await callback.answer()
    await callback.message.answer("Обрабатываю файл и готовлю тест...")

    try:
        text = await get_text_from_pending_file(user_id)
    except Exception as error:
        logging.exception("File processing error")
        await callback.message.answer(f"Ошибка при обработке файла:\n{error}")
        return

    if not text or len(text) < 200:
        await callback.message.answer("Материал слишком короткий. Пришлите более подробный файл.")
        return

    await start_quiz_from_text(callback.message, text, user_id)


@router.callback_query(F.data.startswith("mode_oral:"))
async def mode_oral_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    await callback.answer()
    await callback.message.answer("Обрабатываю файл и начинаю устный опрос...")

    try:
        text = await get_text_from_pending_file(user_id)
    except Exception as error:
        logging.exception("File processing error")
        await callback.message.answer(f"Ошибка при обработке файла:\n{error}")
        return

    if not text or len(text) < 200:
        await callback.message.answer("Материал слишком короткий. Пришлите более подробный файл.")
        return

    await start_oral_from_text(callback.message, text, user_id)


@router.callback_query(F.data.startswith("single:"))
async def single_answer_callback(callback: CallbackQuery) -> None:
    _, session_id, option_index = callback.data.split(":")
    option_index = int(option_index)

    user_id = callback.from_user.id
    session = QUIZ_SESSIONS.get(user_id)

    if not session or session["session_id"] != session_id:
        await callback.answer("Этот вопрос уже неактивен.")
        return

    question = session["quiz"][session["current_index"]]
    options = question.get("options", [])

    if option_index >= len(options):
        await callback.answer("Вариант не найден.")
        return

    user_answer = options[option_index]
    result = answer_result(question.get("type", ""), user_answer, question.get("correct", []))

    await callback.answer()
    await show_answer_and_next(callback.message, user_id, user_answer, result)


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_answer_callback(callback: CallbackQuery) -> None:
    _, session_id, option_index = callback.data.split(":")
    option_index = int(option_index)

    user_id = callback.from_user.id
    session = QUIZ_SESSIONS.get(user_id)

    if not session or session["session_id"] != session_id:
        await callback.answer("Этот вопрос уже неактивен.")
        return

    selected = session["selected"]

    if option_index in selected:
        selected.remove(option_index)
        await callback.answer("Вариант убран")
    else:
        selected.add(option_index)
        await callback.answer("Вариант выбран")


@router.callback_query(F.data.startswith("submit:"))
async def submit_multiple_callback(callback: CallbackQuery) -> None:
    _, session_id = callback.data.split(":")
    user_id = callback.from_user.id
    session = QUIZ_SESSIONS.get(user_id)

    if not session or session["session_id"] != session_id:
        await callback.answer("Этот вопрос уже неактивен.")
        return

    question = session["quiz"][session["current_index"]]
    options = question.get("options", [])

    selected_indexes = sorted(session["selected"])
    user_answers = [options[index] for index in selected_indexes if index < len(options)]

    result = answer_result(question.get("type", ""), user_answers, question.get("correct", []))

    await callback.answer()
    await show_answer_and_next(callback.message, user_id, user_answers, result)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if user_id and user_id in ORAL_SESSIONS:
        await process_oral_answer(message, user_id)
        return

    if user_id and user_id in QUIZ_SESSIONS:
        session = QUIZ_SESSIONS[user_id]

        if session.get("awaiting_text_answer"):
            question = session["quiz"][session["current_index"]]
            question_type = question.get("type", "")
            user_answer = message.text.strip()

            result = answer_result(question_type, user_answer, question.get("correct", []))

            await show_answer_and_next(message, user_id, user_answer, result)
            return

        await message.answer("Пожалуйста, выберите ответ кнопкой под текущим вопросом.")
        return

    await message.answer(START_TEXT)


async def start_http_server() -> None:
    async def health_check(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health_check)

    port = int(os.getenv("PORT", "10000"))

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logging.info("HTTP server started on port %s", port)

    while True:
        await asyncio.sleep(3600)


async def start_bot() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=False)

    webhook_info = await bot.get_webhook_info()
    logging.info("Webhook info: %s", webhook_info)

    logging.info("Telegram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    await asyncio.gather(
        start_http_server(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
