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

QUIZ_STORAGE: dict[str, dict] = {}
USER_TEXT_ANSWERS: dict[int, dict] = {}

START_TEXT = (
    "Здравствуйте. Я помогу закрепить материал урока. "
    "Пришлите объяснение нового материала в аудио формате или формате TXT"
)

AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a",
    ".wav", ".webm", ".ogg", ".oga", ".flac",
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
  "correct": ["А"],
  "explanation": "Короткое объяснение"
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
- В correct указывай точное значение из вариантов или краткий ответ.
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


def make_keyboard(question_id: str, options: list[str]) -> InlineKeyboardMarkup:
    buttons = []

    for index, option in enumerate(options):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=option[:60],
                    callback_data=f"answer:{question_id}:{index}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_question(message: Message, question: dict) -> None:
    question_id = str(uuid4())

    QUIZ_STORAGE[question_id] = {
        "question": question,
        "user_id": message.from_user.id if message.from_user else None,
    }

    number = question.get("number", "?")
    question_text = question.get("question", "")
    options = question.get("options", [])
    question_type = question.get("type", "")

    text = f"<b>Вопрос {number}</b>\n\n{question_text}"

    if question_type == "short_answer":
        USER_TEXT_ANSWERS[message.from_user.id] = {
            "question_id": question_id,
            "correct": question.get("correct", []),
            "explanation": question.get("explanation", ""),
        }

        await message.answer(
            text + "\n\nНапишите ответ одним сообщением. Ответ должен состоять из 1-2 слов."
        )
        return

    await message.answer(
        text,
        reply_markup=make_keyboard(question_id, options),
    )


async def send_quiz(message: Message, text: str) -> None:
    if len(text) > 14000:
        text = text[:14000]

    await message.answer("Материал получен. Готовлю задания...")

    try:
        quiz = await generate_quiz_json(text)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    if not isinstance(quiz, list):
        await message.answer("Ошибка: модель вернула неверный формат заданий.")
        return

    for question in quiz:
        await send_question(message, question)
        await asyncio.sleep(0.4)


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

    await send_quiz(message, text)


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

    await send_quiz(message, text)


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

    await send_quiz(message, text)


@router.callback_query(F.data.startswith("answer:"))
async def answer_callback(callback: CallbackQuery) -> None:
    _, question_id, option_index = callback.data.split(":")
    option_index = int(option_index)

    stored = QUIZ_STORAGE.get(question_id)

    if not stored:
        await callback.answer("Вопрос устарел.")
        return

    question = stored["question"]
    options = question.get("options", [])
    correct = question.get("correct", [])
    explanation = question.get("explanation", "")

    user_answer = options[option_index]

    is_correct = user_answer in correct

    result = "✅ Верно!" if is_correct else "❌ Неверно."

    correct_text = "\n".join(correct)

    await callback.message.answer(
        f"{result}\n\n"
        f"Ваш ответ:\n{user_answer}\n\n"
        f"Правильный ответ:\n{correct_text}\n\n"
        f"{explanation}"
    )

    await callback.answer()


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None

    if user_id in USER_TEXT_ANSWERS:
        data = USER_TEXT_ANSWERS.pop(user_id)

        user_answer = message.text.strip().lower()
        correct_answers = [
            str(answer).strip().lower()
            for answer in data["correct"]
        ]

        is_correct = user_answer in correct_answers

        result = "✅ Верно!" if is_correct else "❌ Неверно."
        correct_text = "\n".join(data["correct"])

        await message.answer(
            f"{result}\n\n"
            f"Ваш ответ:\n{message.text.strip()}\n\n"
            f"Правильный ответ:\n{correct_text}\n\n"
            f"{data['explanation']}"
        )
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
