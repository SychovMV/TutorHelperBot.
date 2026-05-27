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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
USER_LAST_LESSON_TEXT: dict[int, str] = {}

START_TEXT = (
    "Здравствуйте. Я помогу закрепить материал урока. "
    "Пришлите объяснение нового материала в аудио формате или формате TXT \n\n"
    "Пришлите файл в качестве ответа на это сообщение"
)

AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a",
    ".wav", ".webm", ".ogg", ".oga", ".flac",
}


async def generate_quiz_json(text: str) -> list[dict]:
    prompt = f"""
Составь 10 интерактивных заданий по материалу урока.

Верни ТОЛЬКО валидный JSON-массив без markdown, без ```json и без пояснений.

Формат обычного задания:
{{
  "number": 1,
  "type": "single_choice",
  "question": "Текст вопроса",
  "options": ["А. ...", "Б. ...", "В. ...", "Г. ..."],
  "correct": ["А. ..."],
  "explanation": "Короткое объяснение"
}}

Формат задания на соотнесение:
{{
  "number": 6,
  "type": "matching_2",
  "question": "Соотнесите элементы двух списков:\\n\\nСписок 1:\\n1. ...\\n2. ...\\n3. ...\\n4. ...\\n\\n    Список 2:\\nА. ...\\nБ. ...\\nВ. ...\\nГ. ...\\n\\nВведите ответ в формате: 1а 2б 3в 4г",
  "options": [],
  "correct": ["1а 2б 3в 4г"],
  "explanation": "Короткое объяснение правильных соответствий"
}}

Обязательные типы заданий:

1-3: single_choice — один правильный ответ.
- В options дай ровно 4 варианта.
- В correct укажи ровно один вариант, полностью совпадающий с одним из options.

4-5: multiple_choice — несколько правильных ответов.
- В options дай ровно 5 вариантов.
- В correct укажи 2 или 3 правильных варианта.
- Каждый correct должен полностью совпадать с одним из options.

6: matching_2 — соотнести варианты из 2 множеств.
- НЕ делай кнопки.
- options должен быть пустым списком [].
- НЕ делай таблицу со столбцами рядом.
- В question сделай списки друг под другом.
- Первый список обозначай цифрами: 1, 2, 3, 4.
- Второй список обозначай буквами: А, Б, В, Г.
- Второй список должен начинаться после абзацного отступа: \\n\\n    Список 2:
- В конце question напиши: "Введите ответ в формате: 1а 2б 3в 4г".
- В correct укажи одну строку с правильным соответствием, например: ["1а 2в 3б 4г"].

7: matching_3_4 — соотнести варианты из 3-4 множеств.
- НЕ делай кнопки.
- options должен быть пустым списком [].
- НЕ делай таблицу со столбцами рядом.
- Все списки должны идти друг под другом.
- Разные списки разделяй абзацным отступом.
- Первый список обозначай цифрами: 1, 2, 3, 4.
- Второй список обозначай буквами: А, Б, В, Г.
- Третий список обозначай римскими цифрами: I, II, III, IV.
- Если нужен четвёртый список, обозначай его маленькими буквами: а, б, в, г.
- В конце question напиши пример формата ответа, например: "1аI 2бII 3вIII 4гIV".
- В correct укажи одну строку с правильным соответствием.

8: ordering — расположить элементы в хронологической или логической последовательности.
- В question перечисли элементы, которые нужно упорядочить.
- В options дай 4 готовых варианта последовательности.
- В correct укажи один правильный вариант из options.

9: find_errors — найти фрагменты текста с ошибками.
- В question напиши короткий текст на 4-6 предложений и инструкцию: "Выберите фрагменты, содержащие ошибки".
- В тексте должно быть 2-3 фактические ошибки.
- В options дай 4-5 конкретных фрагментов из текста.
- В correct укажи только фрагменты с ошибками.

10: short_answer — вписать правильный ответ самостоятельно.
- В options поставь [].
- В correct укажи только один правильный ответ.
- Правильный ответ ОБЯЗАТЕЛЬНО должен состоять из 1 или 2 слов.
- Нельзя использовать длинные определения.
- Нельзя использовать целые предложения.
- Вопрос должен спрашивать термин, имя, понятие, дату, место или короткое название.

Общие правила:
- Пиши на русском языке.
- Каждый вопрос должен быть понятен школьнику.
- Не выходи за рамки материала.
- Для single_choice, multiple_choice, ordering и find_errors correct должен точно совпадать с options.
- Для matching_2 и matching_3_4 correct должен быть строкой с парами/связками.
- Для matching_2 и matching_3_4 НЕ используй кнопки и НЕ заполняй options.
- Для short_answer правильный ответ должен быть строго 1-2 слова.

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


def count_words(text: str) -> int:
    return len(re.findall(r"[А-Яа-яA-Za-zЁё0-9]+", text))


async def repair_short_answer_question(question: dict, lesson_text: str) -> dict:
    repair_prompt = f"""
Переделай задание short_answer так, чтобы правильный ответ состоял строго из 1 или 2 слов.

Верни ТОЛЬКО JSON-объект без markdown.

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
            {"role": "system", "content": "Ты исправляешь учебное задание в строгом JSON-формате."},
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.2,
        max_tokens=900,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def repair_invalid_short_answers(quiz: list[dict], lesson_text: str) -> list[dict]:
    repaired_quiz = []

    for question in quiz:
        if not isinstance(question, dict):
            continue

        if question.get("type") == "short_answer":
            correct = question.get("correct", [])

            if not isinstance(correct, list):
                correct = [str(correct)]

            correct = [str(item).strip() for item in correct if str(item).strip()]

            if len(correct) != 1 or count_words(correct[0]) < 1 or count_words(correct[0]) > 2:
                try:
                    question = await repair_short_answer_question(question, lesson_text)
                except Exception:
                    logging.exception("Short answer repair error")
                    question = {
                        "number": question.get("number", 10),
                        "type": "short_answer",
                        "question": "Как называется человек, находящийся в собственности другого человека в Древнем Риме?",
                        "options": [],
                        "correct": ["раб"],
                        "explanation": "Раб — это человек, лишённый личной свободы и находящийся в собственности другого человека.",
                    }

        repaired_quiz.append(question)

    return repaired_quiz


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

    if question_type != "short_answer":
        valid_correct = [answer for answer in question["correct"] if answer in question["options"]]
        if valid_correct:
            question["correct"] = valid_correct
        elif question["options"]:
            question["correct"] = [question["options"][0]]

    if question_type == "single_choice":
        question["options"] = question["options"][:4]
        question["correct"] = question["correct"][:1]

    if question_type in {"multiple_choice", "find_errors"}:
        if len(question["correct"]) == 0 and question["options"]:
            question["correct"] = [question["options"][0]]

    if question_type == "ordering":
        question["correct"] = question["correct"][:1]
        if len(question["correct"]) == 0 and question["options"]:
            question["correct"] = [question["options"][0]]

    if question_type == "short_answer":
        question["options"] = []
        question["correct"] = question["correct"][:1]
        if not question["correct"] or count_words(question["correct"][0]) > 2:
            question["correct"] = ["ответ"]
            question["explanation"] = "Модель вернула слишком длинный ответ. Попробуйте отправить материал ещё раз."

    return question


def normalize_quiz(quiz: list[dict]) -> list[dict]:
    normalized = []
    for index, question in enumerate(quiz[:10], start=1):
        if isinstance(question, dict):
            normalized.append(normalize_question(question, index))
    return normalized


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


def get_user_id_from_message(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def make_keyboard(question: dict, session_id: str) -> InlineKeyboardMarkup:
    question_type = question.get("type", "")
    options = question.get("options", [])

    buttons = []

    if question_type in {"multiple_choice", "find_errors"}:
        for index, option in enumerate(options):
            buttons.append([
                InlineKeyboardButton(
                    text=option[:60],
                    callback_data=f"toggle:{session_id}:{index}",
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                text="✅ Ответить",
                callback_data=f"submit:{session_id}",
            )
        ])
    else:
        for index, option in enumerate(options):
            buttons.append([
                InlineKeyboardButton(
                    text=option[:60],
                    callback_data=f"single:{session_id}:{index}",
                )
            ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def finish_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Пройти тест снова",
                    callback_data=f"restart_quiz:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Закрепить материал другого урока",
                    callback_data=f"new_lesson:{user_id}",
                )
            ],
        ]
    )


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
        else:
            text += "\n\nНапишите ответ одним сообщением."

        await message_or_callback.answer(text)
        return

    session["awaiting_text_answer"] = False

    if not options:
        await message_or_callback.answer(
            text + "\n\nДля этого вопроса не удалось создать варианты ответа. Перехожу к следующему."
        )
        session["current_index"] += 1
        await send_current_question(message_or_callback, user_id)
        return

    await message_or_callback.answer(
        text,
        reply_markup=make_keyboard(question, session["session_id"]),
    )


async def show_answer_and_next(message: Message, user_id: int, user_answer, is_correct: bool) -> None:
    session = QUIZ_SESSIONS.get(user_id)

    if not session:
        return

    question = session["quiz"][session["current_index"]]
    correct = question.get("correct", [])
    explanation = question.get("explanation", "")

    result = "✅ Верно!" if is_correct else "❌ Неверно."

    if isinstance(user_answer, list):
        user_answer_text = "\n".join(user_answer) if user_answer else "Ответ не выбран"
    else:
        user_answer_text = str(user_answer)

    correct_text = "\n".join(correct)

    await message.answer(
        f"{result}\n\n"
        f"<b>Ваш ответ:</b>\n{user_answer_text}\n\n"
        f"<b>Правильный ответ:</b>\n{correct_text}\n\n"
        f"<b>Объяснение:</b>\n{explanation}"
    )

    session["answers"].append(
        {
            "question_number": question.get("number", session["current_index"] + 1),
            "is_correct": is_correct,
            "user_answer": user_answer_text,
            "correct": correct_text,
        }
    )

    if is_correct:
        session["score"] += 1

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

    lines = [
        "🏁 <b>Тест завершён!</b>",
        "",
        f"Правильных ответов: <b>{score} из {total}</b>",
        f"Результат: <b>{percent}%</b>",
        "",
        "<b>Подробная статистика:</b>",
    ]

    for item in session["answers"]:
        mark = "✅" if item["is_correct"] else "❌"
        lines.append(f"{mark} Вопрос {item['question_number']}")

    if percent >= 80:
        lines.append("\nОтличный результат!")
    elif percent >= 50:
        lines.append("\nНеплохо, но стоит повторить материал.")
    else:
        lines.append("\nРекомендую ещё раз разобрать тему урока.")

    await message_or_callback.answer(
        "\n".join(lines),
        reply_markup=finish_keyboard(user_id),
    )

    QUIZ_SESSIONS.pop(user_id, None)


async def start_quiz_from_text(message: Message, text: str) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        await message.answer("Не удалось определить пользователя.")
        return

    if len(text) > 14000:
        text = text[:14000]

    USER_LAST_LESSON_TEXT[user_id] = text

    await message.answer("Материал получен. Готовлю тест...")

    try:
        quiz = await generate_quiz_json(text)
        quiz = await repair_invalid_short_answers(quiz, text)
        quiz = normalize_quiz(quiz)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    if not quiz:
        await message.answer("Ошибка: модель вернула неверный формат заданий.")
        return

    QUIZ_SESSIONS[user_id] = {
        "session_id": str(uuid4()),
        "quiz": quiz,
        "current_index": 0,
        "score": 0,
        "answers": [],
        "selected": set(),
        "awaiting_text_answer": False,
    }

    await message.answer("Тест готов. Начинаем!")
    await send_current_question(message, user_id)


async def restart_quiz_from_last_text(callback: CallbackQuery, user_id: int) -> None:
    lesson_text = USER_LAST_LESSON_TEXT.get(user_id)

    if not lesson_text:
        await callback.message.answer(
            "Не нашёл предыдущий материал. Пришлите TXT или аудио ещё раз."
        )
        return

    await callback.message.answer("Создаю новый тест по тому же материалу...")

    try:
        quiz = await generate_quiz_json(lesson_text)
        quiz = await repair_invalid_short_answers(quiz, lesson_text)
        quiz = normalize_quiz(quiz)
    except Exception as error:
        logging.exception("Restart quiz generation error")
        await callback.message.answer(f"Ошибка при генерации нового теста:\n{error}")
        return

    QUIZ_SESSIONS[user_id] = {
        "session_id": str(uuid4()),
        "quiz": quiz,
        "current_index": 0,
        "score": 0,
        "answers": [],
        "selected": set(),
        "awaiting_text_answer": False,
    }

    await callback.message.answer("Новый тест готов. Начинаем!")
    await send_current_question(callback.message, user_id)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(START_TEXT)


@router.callback_query(F.data.startswith("restart_quiz:"))
async def restart_quiz_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    await callback.answer()
    await restart_quiz_from_last_text(callback, user_id)


@router.callback_query(F.data.startswith("new_lesson:"))
async def new_lesson_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    QUIZ_SESSIONS.pop(user_id, None)
    USER_LAST_LESSON_TEXT.pop(user_id, None)

    await callback.answer()
    await callback.message.answer(START_TEXT)


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    document = message.document

    if not document.file_name:
        await message.answer("Не удалось определить имя файла.")
        return

    file_name = document.file_name.lower()
    downloaded_file = await bot.download(document)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать файл.")
        return

    raw_data = downloaded_file.getvalue()

    if file_name.endswith(".txt"):
        try:
            text = raw_data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw_data.decode("cp1251")
            except UnicodeDecodeError:
                await message.answer("Не удалось прочитать TXT-файл. Сохраните его в UTF-8.")
                return

    elif any(file_name.endswith(ext) for ext in AUDIO_EXTENSIONS):
        await message.answer("Аудиофайл получен. Расшифровываю...")

        try:
            text = await transcribe_audio(raw_data, file_name)
        except Exception as error:
            logging.exception("Audio transcription error")
            await message.answer(f"Ошибка при расшифровке аудио:\n{error}")
            return

    else:
        await message.answer("Пришлите файл в формате TXT или аудио.")
        return

    text = text.strip()

    if len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробное объяснение.")
        return

    await start_quiz_from_text(message, text)


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    await message.answer("Голосовое сообщение получено. Расшифровываю...")

    downloaded_file = await bot.download(message.voice)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать голосовое сообщение.")
        return

    try:
        text = await transcribe_audio(downloaded_file.getvalue(), "voice.ogg")
    except Exception as error:
        logging.exception("Voice transcription error")
        await message.answer(f"Ошибка при расшифровке голосового сообщения:\n{error}")
        return

    if len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробное аудио.")
        return

    await start_quiz_from_text(message, text)


@router.message(F.audio)
async def audio_handler(message: Message, bot: Bot) -> None:
    await message.answer("Аудио получено. Расшифровываю...")

    downloaded_file = await bot.download(message.audio)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать аудио.")
        return

    file_name = message.audio.file_name or "audio.mp3"

    try:
        text = await transcribe_audio(downloaded_file.getvalue(), file_name)
    except Exception as error:
        logging.exception("Audio transcription error")
        await message.answer(f"Ошибка при расшифровке аудио:\n{error}")
        return

    if len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробное аудио.")
        return

    await start_quiz_from_text(message, text)


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
    correct = question.get("correct", [])

    if option_index >= len(options):
        await callback.answer("Вариант не найден.")
        return

    user_answer = options[option_index]
    is_correct = user_answer in correct

    await callback.answer()
    await show_answer_and_next(callback.message, user_id, user_answer, is_correct)


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
    correct = question.get("correct", [])

    selected_indexes = sorted(session["selected"])
    user_answers = [options[index] for index in selected_indexes if index < len(options)]

    is_correct = set(user_answers) == set(correct)

    await callback.answer()
    await show_answer_and_next(callback.message, user_id, user_answers, is_correct)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if user_id and user_id in QUIZ_SESSIONS:
        session = QUIZ_SESSIONS[user_id]

        if session.get("awaiting_text_answer"):
            question = session["quiz"][session["current_index"]]
            correct = question.get("correct", [])
            question_type = question.get("type", "")

            user_answer = message.text.strip()

            if question_type in {"matching_2", "matching_3_4"}:
                normalized_user_answer = normalize_matching_answer(user_answer)
                normalized_correct = [
                    normalize_matching_answer(str(answer))
                    for answer in correct
                ]
            else:
                normalized_user_answer = normalize_answer(user_answer)
                normalized_correct = [
                    normalize_answer(str(answer))
                    for answer in correct
                ]

            is_correct = normalized_user_answer in normalized_correct

            await show_answer_and_next(message, user_id, user_answer, is_correct)
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
