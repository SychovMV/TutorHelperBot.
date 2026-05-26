import asyncio
import io
import json
import logging
import os
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

START_TEXT = (
    "Здравствуйте. Я помогу закрепить материал урока. "
    "Пришлите объяснение нового материала в аудио формате или формате TXT"
)

AUDIO_EXTENSIONS = {
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".oga",
    ".flac",
}


async def generate_quiz_json(text: str) -> list[dict]:
    prompt = f"""
Составь 10 заданий по материалу урока.

Верни ТОЛЬКО валидный JSON-массив без markdown и пояснений.

Формат каждого задания:
{{
  "number": 1,
  "type": "single_choice",
  "question": "Текст вопроса",
  "options": ["А. ...", "Б. ...", "В. ...", "Г. ..."],
  "correct": ["А. ..."],
  "explanation": "Короткое объяснение, почему этот ответ правильный"
}}

Типы заданий:
1-3: single_choice — один правильный ответ, 4 варианта.
4-5: multiple_choice — несколько правильных ответов, 5 вариантов.
6: matching_2 — соотнести 2 множества. Дай варианты ответа как готовые соответствия.
7: matching_3_4 — соотнести 3-4 множества. Дай варианты ответа как готовые соответствия.
8: ordering — расположить в логической или хронологической последовательности. Дай варианты последовательностей.
9: find_errors — короткий текст с 2-3 ошибками. В вариантах дай фрагменты, которые пользователь должен выбрать.
10: short_answer — ответ 1-2 слова. В options поставь пустой список, correct содержит правильный ответ.

Правила:
- Пиши на русском языке.
- Каждый вопрос должен быть понятен школьнику.
- В correct указывай точное значение из options.
- Для short_answer correct содержит правильный ответ из 1-2 слов.
- Для multiple_choice и find_errors correct может содержать несколько элементов.
- Не выходи за рамки материала.

Материал:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "Ты создаёшь интерактивные учебные задания в JSON.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0.4,
        max_tokens=3000,
    )

    content = response.choices[0].message.content.strip()
    return json.loads(content)


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


def get_user_id_from_message(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def make_keyboard(question: dict, session_id: str) -> InlineKeyboardMarkup:
    question_type = question.get("type", "")
    options = question.get("options", [])

    buttons = []

    if question_type in {"multiple_choice", "find_errors"}:
        for index, option in enumerate(options):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=option[:60],
                        callback_data=f"toggle:{session_id}:{index}",
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Ответить",
                    callback_data=f"submit:{session_id}",
                )
            ]
        )

    else:
        for index, option in enumerate(options):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=option[:60],
                        callback_data=f"single:{session_id}:{index}",
                    )
                ]
            )

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

    if question_type == "short_answer":
        session["awaiting_text_answer"] = True
        text += "\n\nНапишите ответ одним сообщением. Ответ должен состоять из 1-2 слов."

        await message_or_callback.answer(text)
        return

    session["awaiting_text_answer"] = False

    keyboard = make_keyboard(question, session["session_id"])

    await message_or_callback.answer(text, reply_markup=keyboard)


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
    percent = round(score / total * 100)

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

    await message_or_callback.answer("\n".join(lines))

    QUIZ_SESSIONS.pop(user_id, None)


async def start_quiz_from_text(message: Message, text: str) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        await message.answer("Не удалось определить пользователя.")
        return

    if len(text) > 14000:
        text = text[:14000]

    await message.answer("Материал получен. Готовлю тест...")

    try:
        quiz = await generate_quiz_json(text)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    if not isinstance(quiz, list) or len(quiz) == 0:
        await message.answer("Ошибка: модель вернула неверный формат заданий.")
        return

    quiz = quiz[:10]

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


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(START_TEXT)


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

    user_answers = [
        options[index]
        for index in selected_indexes
        if index < len(options)
    ]

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

            user_answer = message.text.strip()
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

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logging.info("HTTP server started on port %s", port)

    while True:
        await asyncio.sleep(3600)


async def start_bot() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
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
    
