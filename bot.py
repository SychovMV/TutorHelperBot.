import asyncio
import io
import os
import random
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")


router = Router()


def split_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [sentence.strip() for sentence in sentences if len(sentence.strip()) > 30]


def make_questions(text: str) -> list[str]:
    sentences = split_sentences(text)
    words = re.findall(r"[А-Яа-яA-Za-zЁё]{5,}", text)

    if not sentences:
        return ["Текст слишком короткий. Пришлите более содержательный .txt файл."]

    random.shuffle(sentences)
    random.shuffle(words)

    example_sentence = sentences[0]
    keyword = words[0] if words else "ключевое понятие"

    return [
        "1. Открытый вопрос: Какова главная идея текста?",
        f"2. Вопрос на понимание: Почему важно утверждение: «{example_sentence}»?",
        "3. Вопрос с кратким ответом: Какое ключевое понятие раскрывается в тексте?",
        "4. Вопрос на пересказ: Кратко перескажите содержание текста своими словами.",
        "5. Вопрос на анализ: Какие аргументы или причины приводит автор?",
        "6. Вопрос на сравнение: Какие идеи или явления можно сравнить в тексте?",
        "7. Вопрос на вывод: Какой главный вывод можно сделать после прочтения?",
        f"8. Вопрос с пропуском: Заполните пропуск: одно из ключевых слов текста — ________. Подсказка: «{keyword}».",
        f"9. True/False: Верно ли, что текст связан с понятием «{keyword}»? Объясните ответ.",
        "10. Творческий вопрос: Как можно применить идеи текста в реальной жизни?",
    ]


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Пришли мне .txt файл, а я составлю по нему 10 вопросов разных типов."
    )


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    document = message.document

    if not document.file_name or not document.file_name.lower().endswith(".txt"):
        await message.answer("Пожалуйста, пришлите файл именно в формате .txt.")
        return

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

    questions = make_questions(text)
    await message.answer("\n\n".join(questions))


@router.message()
async def other_handler(message: Message) -> None:
    await message.answer("Пришлите .txt файл, и я составлю 10 вопросов по тексту.")


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
