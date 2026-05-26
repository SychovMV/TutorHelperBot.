import asyncio
import io
import logging
import os
import tempfile

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI


load_dotenv()

logging.basicConfig(level=logging.INFO)

print("BOT_TOKEN exists:", bool(os.getenv("BOT_TOKEN")))
print("OPENAI_API_KEY exists:", bool(os.getenv("OPENAI_API_KEY")))
print("OPENAI_ORG exists:", bool(os.getenv("OPENAI_ORG")))
print("PORT:", os.getenv("PORT"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ORG = os.getenv("OPENAI_ORG")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден")


openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    organization=OPENAI_ORG if OPENAI_ORG else None,
)

router = Router()

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


def get_file_extension(file_name: str | None) -> str:
    if not file_name:
        return ".ogg"

    _, extension = os.path.splitext(file_name.lower())
    return extension or ".ogg"


async def generate_tasks_with_gpt(text: str) -> str:
    prompt = f"""
Ты — методист и помощник преподавателя.

На основе объяснения нового материала составь задания для закрепления темы.

Обязательный формат заданий:

1. Вопрос с выбором одного правильного варианта ответа.
   Дай 4 варианта ответа: А, Б, В, Г.
   После вариантов обязательно напиши правильный ответ.

2. Вопрос с выбором одного правильного варианта ответа.
   Дай 4 варианта ответа: А, Б, В, Г.
   После вариантов обязательно напиши правильный ответ.

3. Вопрос с выбором одного правильного варианта ответа.
   Дай 4 варианта ответа: А, Б, В, Г.
   После вариантов обязательно напиши правильный ответ.

4. Вопрос с возможностью выбора нескольких правильных вариантов ответа.
   Дай 5 вариантов ответа.
   После вариантов обязательно напиши все правильные ответы.

5. Вопрос с возможностью выбора нескольких правильных вариантов ответа.
   Дай 5 вариантов ответа.
   После вариантов обязательно напиши все правильные ответы.

6. Задание: соотнести варианты из 2 множеств.
   Сделай левый и правый столбец.
   После задания обязательно напиши правильные соответствия.

7. Задание: соотнести друг с другом варианты из 3-4 множеств.
   Сделай 3 или 4 группы данных.
   После задания обязательно напиши правильные соответствия.

8. Задание: расположить элементы в хронологической или логической последовательности.
   Дай 4-6 элементов.
   После задания обязательно напиши правильную последовательность.

9. Задание с коротким текстом.
   Напиши короткий текст на 4-6 предложений, в котором есть 2-3 фактические ошибки по теме.
   Пользователь должен процитировать фрагменты текста, содержащие ошибки.
   После текста обязательно напиши правильный ответ: какие фрагменты содержат ошибки и почему.

10. Задание: вписать правильный ответ самостоятельно.
    Ответ должен состоять из 1 либо 2 слов.
    После задания обязательно напиши правильный ответ.

Важные требования:
- Пиши на русском языке.
- Не выходи за рамки предоставленного материала.
- Формулируй задания понятно для школьника.
- Не используй слишком сложные термины без необходимости.
- Сохрани нумерацию от 1 до 10.
- После каждого задания обязательно добавляй строку:
  Правильный ответ: ...

Материал урока:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "Ты создаёшь учебные задания для закрепления материала урока.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0.5,
        max_tokens=2200,
    )

    return response.choices[0].message.content.strip()


async def transcribe_audio(file_bytes: bytes, file_name: str) -> str:
    extension = get_file_extension(file_name)

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


async def send_long_message(message: Message, text: str) -> None:
    max_length = 3900

    if len(text) <= max_length:
        await message.answer(text)
        return

    parts = [
        text[i : i + max_length]
        for i in range(0, len(text), max_length)
    ]

    for part in parts:
        await message.answer(part)


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
        await message.answer("TXT-файл получен. Готовлю задания...")

        try:
            text = raw_data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw_data.decode("cp1251")
            except UnicodeDecodeError:
                await message.answer("Не удалось прочитать TXT-файл. Сохраните его в UTF-8.")
                return

    elif any(file_name.endswith(ext) for ext in AUDIO_EXTENSIONS):
        await message.answer("Аудиофайл получен. Сначала расшифровываю его в текст...")

        try:
            text = await transcribe_audio(raw_data, file_name)
        except Exception as error:
            logging.exception("Audio transcription error")
            await message.answer(f"Ошибка при расшифровке аудио:\n{error}")
            return

        if not text:
            await message.answer("Не удалось получить текст из аудио.")
            return

        await message.answer("Аудио расшифровано. Готовлю задания...")

    else:
        await message.answer("Пришлите файл в формате TXT или аудио.")
        return

    text = text.strip()

    if len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите текст или аудио с более подробным объяснением.")
        return

    if len(text) > 14000:
        text = text[:14000]

    try:
        tasks = await generate_tasks_with_gpt(text)
    except Exception as error:
        logging.exception("OpenAI generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    await send_long_message(message, tasks)


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    await message.answer("Голосовое сообщение получено. Расшифровываю...")

    downloaded_file = await bot.download(message.voice)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать голосовое сообщение.")
        return

    raw_data = downloaded_file.getvalue()

    try:
        text = await transcribe_audio(raw_data, "voice.ogg")
    except Exception as error:
        logging.exception("Voice transcription error")
        await message.answer(f"Ошибка при расшифровке голосового сообщения:\n{error}")
        return

    if not text or len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробное аудио.")
        return

    if len(text) > 14000:
        text = text[:14000]

    await message.answer("Голосовое сообщение расшифровано. Готовлю задания...")

    try:
        tasks = await generate_tasks_with_gpt(text)
    except Exception as error:
        logging.exception("OpenAI generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    await send_long_message(message, tasks)


@router.message(F.audio)
async def audio_handler(message: Message, bot: Bot) -> None:
    await message.answer("Аудио получено. Расшифровываю...")

    downloaded_file = await bot.download(message.audio)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать аудио.")
        return

    raw_data = downloaded_file.getvalue()
    file_name = message.audio.file_name or "audio.mp3"

    try:
        text = await transcribe_audio(raw_data, file_name)
    except Exception as error:
        logging.exception("Audio transcription error")
        await message.answer(f"Ошибка при расшифровке аудио:\n{error}")
        return

    if not text or len(text) < 200:
        await message.answer("Материал слишком короткий. Пришлите более подробное аудио.")
        return

    if len(text) > 14000:
        text = text[:14000]

    await message.answer("Аудио расшифровано. Готовлю задания...")

    try:
        tasks = await generate_tasks_with_gpt(text)
    except Exception as error:
        logging.exception("OpenAI generation error")
        await message.answer(f"Ошибка при генерации заданий:\n{error}")
        return

    await send_long_message(message, tasks)


@router.message(F.text)
async def text_handler(message: Message) -> None:
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
