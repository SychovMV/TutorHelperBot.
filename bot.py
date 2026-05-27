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

USER_STATES: dict[int, dict] = {}
QUIZ_SESSIONS: dict[int, dict] = {}
ORAL_SESSIONS: dict[int, dict] = {}
USER_LAST_LESSON_TEXT: dict[int, str] = {}

AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a",
    ".wav", ".webm", ".ogg", ".oga", ".flac",
}


def choose_mode_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Тест", callback_data=f"choose_mode:test:{user_id}")],
            [InlineKeyboardButton(text="🎙 Вопрос-ответ", callback_data=f"choose_mode:oral:{user_id}")],
        ]
    )


def finish_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Пройти тест снова", callback_data=f"restart_quiz:{user_id}")],
            [InlineKeyboardButton(text="📚 Закрепить материал другого урока", callback_data=f"new_lesson:{user_id}")],
        ]
    )


async def ask_first_question(message: Message) -> None:
    user_id = message.from_user.id

    USER_STATES[user_id] = {"step": "choose_mode"}
    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await message.answer(
        "Здравствуйте. Я помогу закрепить материал урока.\n\n"
        "Что вы хотите сделать?",
        reply_markup=choose_mode_keyboard(user_id),
    )


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await ask_first_question(message)


@router.callback_query(F.data.startswith("choose_mode:"))
async def choose_mode_callback(callback: CallbackQuery) -> None:
    _, mode, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer("Эта кнопка не для вас.")
        return

    USER_STATES[user_id] = {
        "step": "waiting_file",
        "mode": mode,
    }

    await callback.answer()

    if mode == "test":
        await callback.message.answer(
            "Хорошо, сделаем тест.\n\n"
            "Теперь пришлите объяснение нового материала в формате TXT или аудио."
        )
    else:
        await callback.message.answer(
            "Хорошо, проведём режим вопрос-ответ.\n\n"
            "Теперь пришлите объяснение нового материала в формате TXT или аудио."
        )


async def extract_text_from_file(file_name: str, raw_data: bytes) -> str:
    file_name = file_name.lower()

    if file_name.endswith(".txt"):
        try:
            return raw_data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return raw_data.decode("cp1251").strip()

    return await transcribe_audio(raw_data, file_name)


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    state = USER_STATES.get(user_id)

    if not state or state.get("step") != "waiting_file":
        await ask_first_question(message)
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

    await process_uploaded_material(message, file_name, downloaded_file.getvalue())


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    state = USER_STATES.get(user_id)

    if not state or state.get("step") != "waiting_file":
        await ask_first_question(message)
        return

    downloaded_file = await bot.download(message.voice)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать голосовое сообщение.")
        return

    await process_uploaded_material(message, "voice.ogg", downloaded_file.getvalue())


@router.message(F.audio)
async def audio_handler(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    state = USER_STATES.get(user_id)

    if not state or state.get("step") != "waiting_file":
        await ask_first_question(message)
        return

    downloaded_file = await bot.download(message.audio)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать аудио.")
        return

    file_name = message.audio.file_name or "audio.mp3"
    await process_uploaded_material(message, file_name, downloaded_file.getvalue())


async def process_uploaded_material(message: Message, file_name: str, raw_data: bytes) -> None:
    user_id = message.from_user.id
    state = USER_STATES.get(user_id, {})
    mode = state.get("mode")

    if mode == "test":
        await message.answer("Файл получен. Обрабатываю материал и готовлю тест...")
    else:
        await message.answer("Файл получен. Обрабатываю материал и готовлю первый вопрос...")

    try:
        text = await extract_text_from_file(file_name, raw_data)
    except Exception as error:
        logging.exception("File processing error")
        await message.answer(f"Ошибка при обработке файла:\n{error}")
        return

    if not text or len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробный файл.")
        return

    if len(text) > 14000:
        text = text[:14000]

    USER_LAST_LESSON_TEXT[user_id] = text

    if mode == "test":
        await start_quiz_from_text(message, text)
    elif mode == "oral":
        await start_oral_from_text(message, text)
    else:
        await ask_first_question(message)


async def generate_quiz_json(text: str) -> list[dict]:
    prompt = f"""
Составь 10 интерактивных заданий по материалу урока.

Верни ТОЛЬКО валидный JSON-массив без markdown.

Формат:
{{
  "number": 1,
  "type": "single_choice",
  "question": "Текст вопроса",
  "options": ["А. ...", "Б. ...", "В. ...", "Г. ..."],
  "correct": ["А. ..."],
  "explanation": "Короткое объяснение"
}}

Типы:
1-3: single_choice.
4-5: multiple_choice.
6: matching_2. options = [], correct = ["1а 2б 3в 4г"].
7: matching_3_4. options = [], correct = ["1аI 2бII 3вIII 4гIV"].
8: ordering.
9: find_errors.
10: short_answer, ответ строго 1-2 слова.

Для matching_2:
- списки должны идти друг под другом;
- первый список: 1, 2, 3, 4;
- второй список: А, Б, В, Г;
- второй список начинай с абзацного отступа: \\n\\n    Список 2:
- инструкцию формата ответа не добавляй, бот добавит её сам.

Для matching_3_4:
- списки должны идти друг под другом;
- разные списки разделяй абзацным отступом;
- инструкцию формата ответа не добавляй, бот добавит её сам.

Для short_answer:
- correct должен быть 1 или 2 слова;
- не используй длинные определения.

Материал:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты создаёшь учебные задания в строгом JSON-формате."},
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

    question["options"] = [str(x).strip() for x in question["options"] if str(x).strip()]
    question["correct"] = [str(x).strip() for x in question["correct"] if str(x).strip()]

    if question["type"] in {"matching_2", "matching_3_4"}:
        question["options"] = []
        question["correct"] = question["correct"][:1]
        return question

    if question["type"] == "short_answer":
        question["options"] = []
        question["correct"] = question["correct"][:1]

        if not question["correct"] or count_words(question["correct"][0]) > 2:
            question["correct"] = ["ответ"]
            question["explanation"] = "Модель вернула слишком длинный ответ."
        return question

    valid_correct = [x for x in question["correct"] if x in question["options"]]

    if valid_correct:
        question["correct"] = valid_correct
    elif question["options"]:
        question["correct"] = [question["options"][0]]

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


async def start_quiz_from_text(message: Message, text: str) -> None:
    user_id = message.from_user.id

    try:
        quiz = await generate_quiz_json(text)
        quiz = normalize_quiz(quiz)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"Ошибка при генерации теста:\n{error}")
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

    USER_STATES[user_id] = {"step": "quiz"}

    await message.answer("Тест готов. Начинаем!")
    await send_current_question(message, user_id)


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

    question_type = question.get("type", "")
    text = f"<b>Вопрос {index + 1} из {len(quiz)}</b>\n\n{question.get('question', '')}"

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
    await message_or_callback.answer(text, reply_markup=make_keyboard(question, session["session_id"]))


async def show_answer_and_next(message: Message, user_id: int, user_answer, is_correct: bool) -> None:
    session = QUIZ_SESSIONS.get(user_id)
    question = session["quiz"][session["current_index"]]

    result = "✅ Верно!" if is_correct else "❌ Неверно."
    correct_text = "\n".join(question.get("correct", []))

    await message.answer(
        f"{result}\n\n"
        f"<b>Ваш ответ:</b>\n{user_answer}\n\n"
        f"<b>Правильный ответ:</b>\n{correct_text}\n\n"
        f"<b>Объяснение:</b>\n{question.get('explanation', '')}"
    )

    session["answers"].append({"is_correct": is_correct})

    if is_correct:
        session["score"] += 1

    session["current_index"] += 1
    session["awaiting_text_answer"] = False

    await asyncio.sleep(0.8)
    await send_current_question(message, user_id)


async def finish_quiz(message_or_callback, user_id: int) -> None:
    session = QUIZ_SESSIONS.get(user_id)

    score = session["score"]
    total = len(session["quiz"])
    percent = round(score / total * 100)

    await message_or_callback.answer(
        f"🏁 <b>Тест завершён!</b>\n\n"
        f"Правильных ответов: <b>{score} из {total}</b>\n"
        f"Результат: <b>{percent}%</b>"
    )

    QUIZ_SESSIONS.pop(user_id, None)
    USER_STATES[user_id] = {"step": "finished"}


async def generate_oral_question(lesson_text: str, history: list[dict]) -> dict:
    prompt = f"""
Ты экзаменатор. Задай следующий вопрос ученику по материалу урока.

Верни ТОЛЬКО JSON:
{{
  "question": "Вопрос ученику"
}}

Правила:
- задай только один вопрос;
- не давай ответ;
- если ученик ошибался, задай проще;
- если отвечал хорошо, задай глубже.

Материал:
{lesson_text[:12000]}

История:
{json.dumps(history, ensure_ascii=False)}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты проводишь устный экзамен."},
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
Оцени ответ ученика.

Верни ТОЛЬКО JSON:
{{
  "score": 0,
  "feedback": "Комментарий",
  "correct_answer": "Пример правильного ответа"
}}

score:
0 — неверно
1 — частично верно
2 — верно

Материал:
{lesson_text[:12000]}

Вопрос:
{question}

Ответ:
{answer}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты проверяешь устный ответ ученика."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=800,
    )

    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


async def start_oral_from_text(message: Message, text: str) -> None:
    user_id = message.from_user.id

    ORAL_SESSIONS[user_id] = {
        "lesson_text": text,
        "history": [],
        "current_question": None,
        "question_count": 0,
        "score": 0,
    }

    USER_STATES[user_id] = {"step": "oral"}

    await message.answer("Начинаем режим вопрос-ответ.")
    await send_next_oral_question(message, user_id)


async def send_next_oral_question(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    if session["question_count"] >= 10:
        await finish_oral(message, user_id)
        return

    data = await generate_oral_question(session["lesson_text"], session["history"])
    question = data.get("question", "Расскажите главное по теме.")

    session["current_question"] = question
    session["question_count"] += 1

    await message.answer(
        f"<b>Вопрос {session['question_count']} из 10</b>\n\n"
        f"{question}\n\n"
        "Ответьте одним сообщением."
    )


async def process_oral_answer(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    answer = message.text.strip()
    question = session["current_question"]

    evaluation = await evaluate_oral_answer(session["lesson_text"], question, answer)

    score = int(evaluation.get("score", 0))
    feedback = evaluation.get("feedback", "")
    correct_answer = evaluation.get("correct_answer", "")

    session["score"] += score
    session["history"].append(
        {
            "question": question,
            "answer": answer,
            "score": score,
        }
    )

    await message.answer(
        f"<b>Оценка:</b> {score}/2\n\n"
        f"<b>Комментарий:</b>\n{feedback}\n\n"
        f"<b>Пример правильного ответа:</b>\n{correct_answer}"
    )

    await asyncio.sleep(0.8)
    await send_next_oral_question(message, user_id)


async def finish_oral(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    score = session["score"]
    total = 20
    percent = round(score / total * 100)

    await message.answer(
        f"🏁 <b>Устный опрос завершён!</b>\n\n"
        f"Результат: <b>{score} из {total}</b>\n"
        f"Процент: <b>{percent}%</b>"
    )

    ORAL_SESSIONS.pop(user_id, None)
    USER_STATES[user_id] = {"step": "finished"}


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
    await show_answer_and_next(callback.message, user_id, "\n".join(user_answers), is_correct)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user_id = message.from_user.id

    if user_id in ORAL_SESSIONS:
        await process_oral_answer(message, user_id)
        return

    if user_id in QUIZ_SESSIONS:
        session = QUIZ_SESSIONS[user_id]

        if session.get("awaiting_text_answer"):
            question = session["quiz"][session["current_index"]]
            correct = question.get("correct", [])
            question_type = question.get("type", "")

            user_answer = message.text.strip()

            if question_type in {"matching_2", "matching_3_4"}:
                normalized_user_answer = normalize_matching_answer(user_answer)
                normalized_correct = [normalize_matching_answer(str(x)) for x in correct]
            else:
                normalized_user_answer = normalize_answer(user_answer)
                normalized_correct = [normalize_answer(str(x)) for x in correct]

            is_correct = normalized_user_answer in normalized_correct

            await show_answer_and_next(message, user_id, user_answer, is_correct)
            return

        await message.answer("Пожалуйста, выберите ответ кнопкой под текущим вопросом.")
        return

    await ask_first_question(message)


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

    logging.info("Telegram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    await asyncio.gather(
        start_http_server(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
