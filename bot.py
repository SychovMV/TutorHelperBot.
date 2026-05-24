import asyncio
import io
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI


load_dotenv()

print("BOT_TOKEN exists:", bool(os.getenv("BOT_TOKEN")))
print("OPENAI_API_KEY exists:", bool(os.getenv("OPENAI_API_KEY")))
print("PORT:", os.getenv("PORT"))

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден в переменных окружения")


router = Router()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def generate_questions_with_gpt(text: str) -> str:
    prompt = f"""
Ты — помощник преподавателя.

По тексту ниже составь ровно 10 вопросов разных типов.

Требования:
1. Вопросы должны быть по содержанию текста.
2. Используй разные типы вопросов:
   - открытый вопрос
   - вопрос на понимание
   - вопрос на анализ
   - вопрос на вывод
   - вопрос на сравнение
   - вопрос с кратким ответом
   - True/False
   - вопрос с пропуском
   - творческий вопрос
   - вопрос на пересказ
3. Не пиши ответы.
4. Нумеруй вопросы от 1 до 10.
5. Пиши на русском языке.

Текст:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "Ты составляешь учебные вопросы по тексту для школьников.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0.7,
        max_tokens=1200,
    )

    return response.choices[0].message.content.strip()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Пришли мне .txt файл, а я составлю по нему 10 вопросов разных типов с помощью ChatGPT."
    )


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    document = message.document

    if not document.file_name or not document.file_name.lower().endswith(".txt"):
        await message.answer("Пожалуйста, пришлите файл именно в формате .txt.")
        return

    await message.answer("Файл получил. Генерирую вопросы...")

    downloaded_file = await bot.download(document)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer("Не удалось скачать файл.")
        return

    raw_data = downloaded_file.getvalue()

    try:
        text = raw_data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_data.decode("cp1251")
        except UnicodeDecodeError:
            await message.answer("Не удалось прочитать файл. Сохраните его в UTF-8.")
            return

    text = text.strip()

    if len(text) < 200:
        await message.answer("Текст слишком короткий. Пришлите файл минимум на 200 символов.")
        return

    if len(text) > 12000:
        text = text[:12000]

    try:
        questions = await generate_questions_with_gpt(text)
    except Exception as error:
        logging.exception("OpenAI error")
        await message.answer(f"Ошибка при обращении к ChatGPT: {error}")
        return

    await message.answer(questions)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user_text = message.text.strip()

    if len(user_text) < 10:
        await message.answer("Пришлите .txt файл или задайте более подробный вопрос.")
        return

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "Ты полезный помощник. Отвечай кратко и понятно на русском языке.",
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            temperature=0.7,
            max_tokens=800,
        )

        answer = response.choices[0].message.content.strip()
        await message.answer(answer)

    except Exception as error:
        logging.exception("OpenAI text answer error")
        await message.answer(f"Ошибка при обращении к ChatGPT: {error}")


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

    logging.info("HTTP server started on 0.0.0.0:%s", port)

    while True:
        await asyncio.sleep(3600)


async def start_bot() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Telegram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    await asyncio.gather(
        start_http_server(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
