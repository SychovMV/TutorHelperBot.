import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import ClientSession, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from mutagen import File as MutagenFile
from openai import AsyncOpenAI


load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

SQLITE_PATH = os.getenv("SQLITE_PATH", "bot.sqlite3")
USED_USERS_URL = os.getenv("USED_USERS_URL", "").strip()
PROMO_CODES_URL = os.getenv("PROMO_CODES_URL", "").strip()

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "275036391"))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
PATREON_CLIENT_ID = os.getenv("PATREON_CLIENT_ID", "").strip()
PATREON_CLIENT_SECRET = os.getenv("PATREON_CLIENT_SECRET", "").strip()
PATREON_REDIRECT_URI = os.getenv("PATREON_REDIRECT_URI", "").strip()
PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID", "").strip()
PATREON_JOIN_URL = os.getenv("PATREON_JOIN_URL", "https://www.patreon.com/").strip()
PATREON_WEBHOOK_SECRET = os.getenv("PATREON_WEBHOOK_SECRET", "").strip()
PATREON_TIER_LIMITS = json.loads(os.getenv("PATREON_TIER_LIMITS_JSON", "{}"))

PATREON_AUTH_URL = "https://www.patreon.com/oauth2/authorize"
PATREON_TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
PATREON_IDENTITY_URL = "https://www.patreon.com/api/oauth2/v2/identity"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
router = Router()
BOT_INSTANCE: Bot | None = None

QUIZ_SESSIONS: dict[int, dict] = {}
ORAL_SESSIONS: dict[int, dict] = {}
USER_LAST_LESSON_TEXT: dict[int, str] = {}
USER_PENDING_FILES: dict[int, dict] = {}

START_TEXT_RU = (
    "Здравствуйте. Я помогу закрепить материал урока. "
    "Пришлите объяснение нового материала в аудио, видео формате или формате TXT \n\n"
    "Пришлите файл в качестве ответа на это сообщение"
)

START_TEXT_EN = (
    "Hello. I will help reinforce the lesson material. "
    "Send the explanation of the new material as audio, video, or TXT.\n\n"
    "Send the file as a reply to this message."
)

def get_chat_lang(chat_id: int | str | None) -> str:
    if chat_id is None:
        return "en"

    try:
        chat_id_int = int(chat_id)
    except Exception:
        return "en"

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            row = conn.execute(
                "SELECT language FROM chat_settings WHERE chat_id = ?",
                (chat_id_int,),
            ).fetchone()
    except Exception:
        return "en"

    if row and str(row[0]).lower() == "ru":
        return "ru"

    return "en"


def set_chat_lang(chat_id: int | str | None, language: str) -> None:
    if chat_id is None:
        return

    try:
        chat_id_int = int(chat_id)
    except Exception:
        return

    language = "ru" if str(language).lower().startswith("ru") else "en"

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chat_settings (chat_id, language, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                language = excluded.language,
                updated_at = excluded.updated_at
            """,
            (chat_id_int, language, now_iso()),
        )
        conn.commit()


def get_ui_lang_from_message(message: Message) -> str:
    return get_chat_lang(message.chat.id)


def ui_text_from_chat(chat_id: int | str | None, ru: str, en: str) -> str:
    return ru if get_chat_lang(chat_id) == "ru" else en


def ui_text_from_user(user, ru: str, en: str) -> str:
    return en


def ui_text(message: Message, ru: str, en: str) -> str:
    return ui_text_from_chat(message.chat.id, ru, en)


def start_text_for_chat(chat_id: int | str | None) -> str:
    return ui_text_from_chat(chat_id, START_TEXT_RU, START_TEXT_EN)


def start_text_for_user(user) -> str:
    return START_TEXT_EN


def is_ru_user(user) -> bool:
    return False


def language_keyboard(chat_id: int | str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=ui_text_from_chat(chat_id, "🌐 Сменить язык", "🌐 Change language"),
                callback_data="switch_language"
            )]
        ]
    )


AUDIO_EXTENSIONS = {
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a",
    ".wav", ".webm", ".ogg", ".oga", ".flac",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def init_db() -> None:
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True) if os.path.dirname(SQLITE_PATH) else None

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                credits INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                text_uploads_count INTEGER DEFAULT 0,
                audio_uploads_count INTEGER DEFAULT 0,
                patreon_user_id TEXT DEFAULT '',
                patreon_member_id TEXT DEFAULT '',
                patreon_status TEXT DEFAULT 'none',
                patreon_tier_id TEXT DEFAULT '',
                patreon_tier_title TEXT DEFAULT '',
                patreon_access_token TEXT DEFAULT '',
                patreon_refresh_token TEXT DEFAULT '',
                patreon_token_expires_at INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patreon_oauth_states (
                state TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patreon_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                month_key TEXT NOT NULL,
                text_used INTEGER DEFAULT 0,
                audio_used INTEGER DEFAULT 0,
                updated_at TEXT,
                UNIQUE(telegram_id, month_key)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                credits INTEGER DEFAULT 1,
                used_at TEXT,
                UNIQUE(telegram_id, code)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'en',
                updated_at TEXT
            )
            """
        )

        conn.commit()

    migrate_db()


def migrate_db() -> None:
    required_columns = {
        "users": {
            "credits": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'active'",
            "text_uploads_count": "INTEGER DEFAULT 0",
            "audio_uploads_count": "INTEGER DEFAULT 0",
            "patreon_user_id": "TEXT DEFAULT ''",
            "patreon_member_id": "TEXT DEFAULT ''",
            "patreon_status": "TEXT DEFAULT 'none'",
            "patreon_tier_id": "TEXT DEFAULT ''",
            "patreon_tier_title": "TEXT DEFAULT ''",
            "patreon_access_token": "TEXT DEFAULT ''",
            "patreon_refresh_token": "TEXT DEFAULT ''",
            "patreon_token_expires_at": "INTEGER DEFAULT 0",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }
    }

    with sqlite3.connect(SQLITE_PATH) as conn:
        for table_name, columns in required_columns.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

            for column_name, column_type in columns.items():
                if column_name not in existing:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

        conn.commit()


def ensure_user_in_db(telegram_id: int) -> None:
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (
                telegram_id,
                credits,
                status,
                text_uploads_count,
                audio_uploads_count,
                created_at,
                updated_at
            )
            VALUES (?, 0, 'active', 0, 0, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (telegram_id, now_iso(), now_iso()),
        )
        conn.commit()


def get_user_row(telegram_id: int) -> dict:
    ensure_user_in_db(telegram_id)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    return dict(row)


def update_user_patreon(
    telegram_id: int,
    patreon_user_id: str,
    patreon_member_id: str,
    patreon_status: str,
    patreon_tier_id: str,
    patreon_tier_title: str,
    access_token: str = "",
    refresh_token: str = "",
    expires_at: int = 0,
) -> None:
    ensure_user_in_db(telegram_id)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            UPDATE users
            SET patreon_user_id = ?,
                patreon_member_id = ?,
                patreon_status = ?,
                patreon_tier_id = ?,
                patreon_tier_title = ?,
                patreon_access_token = COALESCE(NULLIF(?, ''), patreon_access_token),
                patreon_refresh_token = COALESCE(NULLIF(?, ''), patreon_refresh_token),
                patreon_token_expires_at = CASE WHEN ? > 0 THEN ? ELSE patreon_token_expires_at END,
                status = CASE WHEN ? = 'active_patron' THEN 'patreon' ELSE status END,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                patreon_user_id,
                patreon_member_id,
                patreon_status,
                patreon_tier_id,
                patreon_tier_title,
                access_token,
                refresh_token,
                expires_at,
                expires_at,
                patreon_status,
                now_iso(),
                telegram_id,
            ),
        )
        conn.commit()


def find_user_by_patreon_member_id(member_id: str) -> int | None:
    if not member_id:
        return None

    with sqlite3.connect(SQLITE_PATH) as conn:
        row = conn.execute(
            "SELECT telegram_id FROM users WHERE patreon_member_id = ?",
            (member_id,),
        ).fetchone()

    return int(row[0]) if row else None


def save_oauth_state(telegram_id: int, state: str) -> None:
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO patreon_oauth_states (state, telegram_id, created_at) VALUES (?, ?, ?)",
            (state, telegram_id, int(time.time())),
        )
        conn.commit()


def pop_oauth_state(state: str) -> int | None:
    with sqlite3.connect(SQLITE_PATH) as conn:
        row = conn.execute(
            "SELECT telegram_id, created_at FROM patreon_oauth_states WHERE state = ?",
            (state,),
        ).fetchone()

        conn.execute("DELETE FROM patreon_oauth_states WHERE state = ?", (state,))
        conn.commit()

    if not row:
        return None

    telegram_id, created_at = int(row[0]), int(row[1])

    if int(time.time()) - created_at > 3600:
        return None

    return telegram_id


def mark_user_used(telegram_id: int, file_kind: str) -> None:
    ensure_user_in_db(telegram_id)
    field = "audio_uploads_count" if file_kind == "audio" else "text_uploads_count"

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            f"""
            UPDATE users
            SET {field} = {field} + 1,
                status = 'used',
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (now_iso(), telegram_id),
        )
        conn.commit()


def local_user_has_used_bot(telegram_id: int) -> bool:
    row = get_user_row(telegram_id)

    return (
        int(row.get("text_uploads_count", 0)) > 0
        or int(row.get("audio_uploads_count", 0)) > 0
        or row.get("status") in {"used", "promo", "patreon"}
    )


async def fetch_json_from_url(url: str) -> dict:
    if not url:
        return {}

    async with ClientSession() as session:
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                logging.warning("GitHub JSON fetch failed: %s %s", response.status, url)
                return {}

            return await response.json()


async def github_user_has_used_bot(telegram_id: int) -> bool:
    data = await fetch_json_from_url(USED_USERS_URL)
    used_users = data.get("used_users", [])

    try:
        used_users = [int(item) for item in used_users]
    except Exception:
        used_users = []

    return telegram_id in used_users


async def user_has_used_bot_anywhere(telegram_id: int) -> bool:
    if local_user_has_used_bot(telegram_id):
        return True

    return await github_user_has_used_bot(telegram_id)


async def get_promo_codes() -> list[dict]:
    data = await fetch_json_from_url(PROMO_CODES_URL)
    promo_codes = data.get("promo_codes", [])

    return promo_codes if isinstance(promo_codes, list) else []


def add_user_credits(telegram_id: int, credits: int) -> None:
    ensure_user_in_db(telegram_id)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ?, updated_at = ? WHERE telegram_id = ?",
            (credits, now_iso(), telegram_id),
        )
        conn.commit()


def spend_user_credit(telegram_id: int) -> bool:
    ensure_user_in_db(telegram_id)

    with sqlite3.connect(SQLITE_PATH) as conn:
        row = conn.execute("SELECT credits FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        credits = int(row[0]) if row else 0

        if credits <= 0:
            return False

        conn.execute(
            "UPDATE users SET credits = credits - 1, updated_at = ? WHERE telegram_id = ?",
            (now_iso(), telegram_id),
        )
        conn.commit()

    return True


async def apply_promo_code(telegram_id: int, code: str) -> tuple[bool, str]:
    code = code.strip().upper()

    if not code:
        return False, "Промокод пустой."

    promo_codes = await get_promo_codes()
    found = None

    for item in promo_codes:
        item_code = str(item.get("code", "")).strip().upper()
        active = bool(item.get("active", False))

        if item_code == code and active:
            found = item
            break

    if not found:
        return False, "Промокод не найден или уже не активен."

    credits = int(found.get("credits", 1))

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(
                "INSERT INTO promo_usages (telegram_id, code, credits, used_at) VALUES (?, ?, ?, ?)",
                (telegram_id, code, credits, now_iso()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "Вы уже использовали этот промокод."

    add_user_credits(telegram_id, credits)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            "UPDATE users SET status = 'promo', updated_at = ? WHERE telegram_id = ?",
            (now_iso(), telegram_id),
        )
        conn.commit()

    return True, f"Промокод принят. Начислено кредитов: {credits}."


def get_monthly_usage(telegram_id: int) -> dict:
    month_key = current_month_key()

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO patreon_usage (telegram_id, month_key, text_used, audio_used, updated_at)
            VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(telegram_id, month_key) DO NOTHING
            """,
            (telegram_id, month_key, now_iso()),
        )
        conn.commit()

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM patreon_usage WHERE telegram_id = ? AND month_key = ?",
            (telegram_id, month_key),
        ).fetchone()

    return dict(row)


def increment_monthly_usage(telegram_id: int, file_kind: str, amount: int = 1) -> None:
    month_key = current_month_key()
    field = "audio_used" if file_kind == "audio" else "text_used"
    get_monthly_usage(telegram_id)

    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute(
            f"""
            UPDATE patreon_usage
            SET {field} = {field} + ?,
                updated_at = ?
            WHERE telegram_id = ? AND month_key = ?
            """,
            (amount, now_iso(), telegram_id, month_key),
        )
        conn.commit()


def get_tier_limits(tier_id: str) -> dict:
    tier_id = str(tier_id or "")

    if tier_id not in PATREON_TIER_LIMITS:
        return {
            "title": "Неизвестный уровень",
            "text_limit": 0,
            "audio_limit": 0,
        }

    return PATREON_TIER_LIMITS[tier_id]


def has_active_patreon_access(telegram_id: int, file_kind: str, amount: int = 1) -> tuple[bool, str]:
    row = get_user_row(telegram_id)
    status = row.get("patreon_status", "")
    tier_id = row.get("patreon_tier_id", "")

    if status != "active_patron":
        return False, "Активная подписка Patreon не найдена."

    limits = get_tier_limits(tier_id)
    usage = get_monthly_usage(telegram_id)

    if file_kind == "audio":
        used = int(usage.get("audio_used", 0))
        limit = int(limits.get("audio_limit", 0))
        label = "аудио"
    else:
        used = int(usage.get("text_used", 0))
        limit = int(limits.get("text_limit", 0))
        label = "TXT"

    if used + amount > limit:
        remaining = max(0, limit - used)
        return False, (
            f"Лимит уровня {limits.get('title')} недостаточен. "
            f"Осталось: {remaining}/{limit} {label} за месяц. "
            f"Нужно для этого файла: {amount}."
        )

    return True, (
        f"Patreon подтверждён: {limits.get('title')}. "
        f"Будет использовано: {used + amount}/{limit} {label} за месяц."
    )


def build_patreon_oauth_url(telegram_id: int) -> str:
    state = str(uuid4())
    save_oauth_state(telegram_id, state)

    params = {
        "response_type": "code",
        "client_id": PATREON_CLIENT_ID,
        "redirect_uri": PATREON_REDIRECT_URI,
        "scope": "identity identity.memberships",
        "state": state,
    }

    return f"{PATREON_AUTH_URL}?{urlencode(params)}"


async def exchange_patreon_code(code: str) -> dict:
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": PATREON_CLIENT_ID,
        "client_secret": PATREON_CLIENT_SECRET,
        "redirect_uri": PATREON_REDIRECT_URI,
    }

    async with ClientSession() as session:
        async with session.post(PATREON_TOKEN_URL, data=payload, timeout=30) as response:
            text = await response.text()

            if response.status != 200:
                raise RuntimeError(f"Patreon token error {response.status}: {text}")

            return json.loads(text)


async def get_patreon_identity(access_token: str) -> dict:
    params = {
        "include": "memberships,memberships.currently_entitled_tiers,memberships.campaign",
        "fields[user]": "full_name,email",
        "fields[member]": "patron_status,last_charge_status,currently_entitled_amount_cents",
        "fields[tier]": "title,amount_cents",
        "fields[campaign]": "creation_name",
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "TutorHelperBot",
    }

    async with ClientSession() as session:
        async with session.get(PATREON_IDENTITY_URL, params=params, headers=headers, timeout=30) as response:
            text = await response.text()

            if response.status != 200:
                raise RuntimeError(f"Patreon identity error {response.status}: {text}")

            return json.loads(text)


def parse_patreon_identity(identity: dict) -> dict:
    patreon_user_id = identity.get("data", {}).get("id", "")
    included = identity.get("included", [])

    campaigns = {
        item.get("id"): item
        for item in included
        if item.get("type") == "campaign"
    }

    tiers = {
        item.get("id"): item
        for item in included
        if item.get("type") == "tier"
    }

    memberships = [
        item
        for item in included
        if item.get("type") == "member"
    ]

    selected_member = None

    for member in memberships:
        campaign_data = (
            member.get("relationships", {})
            .get("campaign", {})
            .get("data", {})
        )
        campaign_id = str(campaign_data.get("id", ""))

        if not PATREON_CAMPAIGN_ID or campaign_id == str(PATREON_CAMPAIGN_ID):
            selected_member = member
            break

    if not selected_member:
        return {
            "patreon_user_id": patreon_user_id,
            "patreon_member_id": "",
            "patreon_status": "none",
            "tier_id": "",
            "tier_title": "",
        }

    member_id = selected_member.get("id", "")
    member_status = selected_member.get("attributes", {}).get("patron_status", "none")

    tier_items = (
        selected_member.get("relationships", {})
        .get("currently_entitled_tiers", {})
        .get("data", [])
    )

    tier_id = ""
    tier_title = ""

    if tier_items:
        tier_id = str(tier_items[0].get("id", ""))
        tier_title = tiers.get(tier_id, {}).get("attributes", {}).get("title", "")

    return {
        "patreon_user_id": patreon_user_id,
        "patreon_member_id": member_id,
        "patreon_status": member_status,
        "tier_id": tier_id,
        "tier_title": tier_title,
    }


def patreon_keyboard(user_id: int, chat_id=None) -> InlineKeyboardMarkup:
    oauth_url = build_patreon_oauth_url(user_id)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "✅ Проверить существующую подписку Patreon", "✅ Check existing Patreon subscription"), url=oauth_url)],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "💜 Стать патроном и получить доступ", "💜 Become a patron and get access"), url=PATREON_JOIN_URL)],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "🌐 Сменить язык", "🌐 Change language"), callback_data="switch_language")],
        ]
    )


def mode_keyboard(user_id: int, chat_id=None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "📝 Тест", "📝 Test"), callback_data=f"mode_test:{user_id}")],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "🎙 Вопрос-ответ", "🎙 Q&A"), callback_data=f"mode_oral:{user_id}")],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "🌐 Сменить язык", "🌐 Change language"), callback_data="switch_language")],
        ]
    )


def finish_keyboard(user_id: int, chat_id=None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "🔁 Пройти тест снова", "🔁 Take the test again"), callback_data=f"restart_quiz:{user_id}")],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "📚 Закрепить материал другого урока", "📚 Reinforce another lesson"), callback_data=f"new_lesson:{user_id}")],
            [InlineKeyboardButton(text=ui_text_from_chat(chat_id, "🌐 Сменить язык", "🌐 Change language"), callback_data="switch_language")],
        ]
    )


async def check_access_gate(message: Message, user_id: int, file_kind: str) -> bool:
    ensure_user_in_db(user_id)

    if file_kind == "text":
        already_used = await user_has_used_bot_anywhere(user_id)

        if not already_used:
            mark_user_used(user_id, file_kind)
            await message.answer(
                ui_text(message, "Пробное использование принято. Выберите режим работы:", "Trial use accepted. Choose a mode:"),
                reply_markup=mode_keyboard(user_id, message.chat.id),
            )
            return True

    if spend_user_credit(user_id):
        mark_user_used(user_id, file_kind)
        await message.answer(
            ui_text(message, "Использован 1 кредит. Выберите режим работы:", "1 credit used. Choose a mode:"),
            reply_markup=mode_keyboard(user_id, message.chat.id),
        )
        return True

    usage_amount = get_pending_file_usage_amount(user_id, file_kind)
    ok, reason = has_active_patreon_access(user_id, file_kind, usage_amount)

    if ok:
        mark_user_used(user_id, file_kind)
        increment_monthly_usage(user_id, file_kind, usage_amount)
        await message.answer(
            f"{reason}\n\n{ui_text(message, 'Выберите режим работы:', 'Choose a mode:')}",
            reply_markup=mode_keyboard(user_id, message.chat.id),
        )
        return True

    await message.answer(
        ui_text(message,
            "Для продолжения нужна активная подписка Patreon.\n\n"
            f"{reason}\n\n"
            "Нажмите кнопку ниже, войдите в Patreon и разрешите боту проверить ваш уровень подписки.\n\n"
            "Для проверки подписки бот запрашивает доступ к данным Patreon. Бот не получает доступ к паролю или платёжным данным и использует информацию только для проверки уровня подписки.\n\n"
            "Также можно ввести промокод:\n"
            "<code>/promo ВАШ_ПРОМОКОД</code>",
            "An active Patreon subscription is required to continue.\n\n"
            f"{reason}\n\n"
            "Press the button below, log in to Patreon, and allow the bot to check your subscription tier.\n\n"
            "To check the subscription, the bot requests access to Patreon data. The bot does not get access to your password or payment data and uses the information only to check your subscription tier.\n\n"
            "You can also enter a promo code:\n"
            "<code>/promo YOUR_PROMO_CODE</code>"
        ),
        reply_markup=patreon_keyboard(user_id, message.chat.id),
    )

    return False


async def unlock_after_patreon_or_promo(message: Message, user_id: int) -> None:
    file_data = USER_PENDING_FILES.get(user_id)

    if not file_data:
        await message.answer(ui_text(message, "Доступ получен, но файл не найден. Пришлите файл заново.", "Access was granted, but the file was not found. Please send the file again."))
        return

    file_kind = file_data.get("file_kind", "text")

    ok, reason = has_active_patreon_access(user_id, file_kind)

    if ok:
        mark_user_used(user_id, file_kind)
        increment_monthly_usage(user_id, file_kind)
        await message.answer(
            f"{reason}\n\n{ui_text(message, 'Выберите режим работы:', 'Choose a mode:')}",
            reply_markup=mode_keyboard(user_id, message.chat.id),
        )
        return

    if spend_user_credit(user_id):
        mark_user_used(user_id, file_kind)
        await message.answer(
            ui_text(message, "Использован 1 кредит. Выберите режим работы:", "1 credit used. Choose a mode:"),
            reply_markup=mode_keyboard(user_id, message.chat.id),
        )
        return

    await message.answer(ui_text(message, "Доступ пока не подтверждён.", "Access has not been confirmed yet."))


async def forward_file_to_admin(bot: Bot, file_bytes: bytes, file_name: str, caption: str) -> None:
    try:
        document = BufferedInputFile(file_bytes, filename=file_name)
        await bot.send_document(
            ADMIN_CHAT_ID,
            document=document,
            caption=caption,
        )
    except Exception:
        logging.exception("Admin file forwarding error")


async def send_sqlite_to_admin(bot: Bot, caption: str = "SQLite база бота") -> None:
    if not os.path.exists(SQLITE_PATH):
        init_db()

    with open(SQLITE_PATH, "rb") as file:
        db_bytes = file.read()

    await forward_file_to_admin(
        bot=bot,
        file_bytes=db_bytes,
        file_name=os.path.basename(SQLITE_PATH),
        caption=caption,
    )


def is_audio_file(file_name: str) -> bool:
    return any(file_name.lower().endswith(ext) for ext in AUDIO_EXTENSIONS)


def get_file_kind(file_name: str) -> str:
    return "text" if file_name.lower().endswith(".txt") else "audio"


def format_duration(seconds: float | None, lang: str = "ru") -> str:
    if seconds is None:
        return "не удалось определить" if lang == "ru" else "could not determine"

    total_seconds = int(round(seconds))
    minutes = total_seconds // 60
    seconds_left = total_seconds % 60

    if minutes == 0:
        return f"{seconds_left} сек." if lang == "ru" else f"{seconds_left} sec."

    return f"{minutes} мин. {seconds_left} сек." if lang == "ru" else f"{minutes} min. {seconds_left} sec."


def get_audio_duration(file_bytes: bytes, file_name: str) -> float | None:
    _, extension = os.path.splitext(file_name.lower())

    if extension not in AUDIO_EXTENSIONS:
        extension = ".ogg"

    try:
        with tempfile.NamedTemporaryFile(suffix=extension, delete=True) as temp_audio:
            temp_audio.write(file_bytes)
            temp_audio.flush()

            audio = MutagenFile(temp_audio.name)

            if audio and audio.info and hasattr(audio.info, "length"):
                return float(audio.info.length)

    except Exception:
        logging.exception("Audio duration detection error")

    return None

def get_pending_file_usage_amount(user_id: int, file_kind: str) -> int:
    if file_kind != "audio":
        return 1

    file_data = USER_PENDING_FILES.get(user_id)

    if not file_data:
        return 1

    duration = get_audio_duration(
        file_data["raw_data"],
        file_data["file_name"]
    )

    if duration is None:
        return 1

    return max(1, int((duration + 59) // 60))


async def transcribe_audio(file_bytes: bytes, file_name: str) -> str:
    _, extension = os.path.splitext(file_name.lower())

    if extension not in AUDIO_EXTENSIONS:
        extension = ".ogg"

    with tempfile.NamedTemporaryFile(suffix=extension, delete=True) as temp_audio:
        temp_audio.write(file_bytes)
        temp_audio.flush()

        with open(temp_audio.name, "rb") as audio_file:
            transcription = await openai_client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
            )

    return transcription.text.strip()


async def send_transcription_file(message: Message, text: str) -> None:
    file = BufferedInputFile(
        text.encode("utf-8"),
        filename="transcription.txt",
    )

    await message.answer_document(
        document=file,
        caption=ui_text(message, "Готово. Вот TXT-файл с расшифровкой аудио.", "Done. Here is the TXT file with the audio transcription."),
    )

    if BOT_INSTANCE:
        await forward_file_to_admin(
            bot=BOT_INSTANCE,
            file_bytes=text.encode("utf-8"),
            file_name="transcription.txt",
            caption=f"Расшифровка аудио от пользователя {message.chat.id}",
        )


async def get_text_after_mode_choice(message: Message, user_id: int) -> str | None:
    file_data = USER_PENDING_FILES.get(user_id)

    if not file_data:
        await message.answer(ui_text(message, "Файл не найден. Пришлите файл заново.", "File not found. Please send the file again."))
        return None

    file_name = file_data["file_name"]
    raw_data = file_data["raw_data"]

    if file_name.endswith(".txt"):
        try:
            return raw_data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return raw_data.decode("cp1251").strip()

    if is_audio_file(file_name):
        duration = get_audio_duration(raw_data, file_name)
        await message.answer(f"{ui_text(message, 'Длительность аудио:', 'Audio duration:')} {format_duration(duration, get_ui_lang_from_message(message))}")
        await message.answer(ui_text(message, "Расшифровываю аудио...", "Transcribing audio..."))

        text = await transcribe_audio(raw_data, file_name)

        if text:
            await send_transcription_file(message, text)

        return text

    await message.answer(ui_text(message, "Поддерживаются только TXT и аудиофайлы.", "Only TXT and audio files are supported."))
    return None


async def generate_quiz_json(text: str) -> list[dict]:
    prompt = f"""
Составь 10 интерактивных заданий по материалу урока.

Все вопросы, варианты ответов, правильные ответы и объяснения должны быть на том же языке, на котором написан материал урока.

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
4-5: multiple_choice.
6: matching_2.
7: matching_3_4.
8: ordering.
9: find_errors.
10: short_answer.

Для ordering и find_errors обязательно сделай options.
Для matching_2 и matching_3_4 options должен быть [].
Для matching_2 и matching_3_4 весь материал для сопоставления обязательно пиши прямо в question:
- сначала список 1, 2, 3, 4;
- затем список а, б, в, г;
- correct должен быть одной строкой формата "1а 2б 3в 4г" или "1аI 2бII 3вIII 4гIV".
Нельзя писать только "Сопоставьте..." без самих элементов для сопоставления.
Для short_answer correct должен состоять из 1 или 2 слов.

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
- Для ordering нужно ровно 4 options, каждый option — полная последовательность.
- Для find_errors нужно 4-5 options.
- Все correct должны полностью совпадать с элементами options.
- options нельзя оставлять пустым.

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
Вопрос задавай на том же языке, на котором написан материал урока.

Верни ТОЛЬКО JSON-объект:
{{
  "question": "Следующий вопрос ученику",
  "reason": "Почему ты задаёшь именно этот вопрос"
}}

Правила:
- Задай только один вопрос.
- Не давай ответ.
- Не составляй список вопросов заранее.
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
Пиши feedback, correct_answer и what_to_ask_next на том же языке, на котором написан материал урока.

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

            if need_repair:
                try:
                    question = await repair_button_question(question, lesson_text)
                except Exception:
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
                        "explanation": "Вопрос был автоматически исправлен.",
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


async def copy_answer_to_admin(
    message: Message,
    text: str,
    target_user_id: int | None = None,
    target_username: str | None = None,
) -> None:
    if not BOT_INSTANCE:
        return

    user = message.from_user

    user_id = target_user_id or (user.id if user else message.chat.id)

    if target_username:
        username = f"@{target_username}"
    elif user and user.username:
        username = f"@{user.username}"
    else:
        username = "username не указан"

    admin_text = (
        "<b>Ответ бота пользователю</b>\n\n"
        f"<b>Пользователь:</b> {user_id} {username}\n"
        f"<b>Диалог:</b> {message.chat.id}\n\n"
        f"<b>Текст ответа:</b>\n{text}"
    )

    try:
        await BOT_INSTANCE.send_message(ADMIN_CHAT_ID, admin_text)
    except Exception:
        logging.exception("Admin answer copy error")


async def monitored_answer(
    message: Message,
    text: str,
    target_user_id: int | None = None,
    target_username: str | None = None,
    **kwargs,
):
    sent_message = await message.answer(text, **kwargs)
    await copy_answer_to_admin(
        message=message,
        text=text,
        target_user_id=target_user_id,
        target_username=target_username,
    )
    return sent_message

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
            text += "\n\n" + ui_text(message_or_callback, "Напишите ответ одним сообщением. Ответ должен состоять из 1-2 слов.", "Write the answer in one message. The answer should contain 1-2 words.")
        elif question_type == "matching_2":
            text += "\n\n" + ui_text(message_or_callback, "Введите ответ в формате: 1а 2б 3в 4г", "Enter the answer in this format: 1a 2b 3c 4d")
        elif question_type == "matching_3_4":
            text += "\n\n" + ui_text(message_or_callback, "Введите ответ в формате: 1аI 2бII 3вIII 4гIV", "Enter the answer in this format: 1aI 2bII 3cIII 4dIV")

        await message_or_callback.answer(text)
        return

    session["awaiting_text_answer"] = False

    if not options:
        await message_or_callback.answer(
            text + "\n\n" + ui_text(message_or_callback, "У этого вопроса не были созданы варианты ответа. Перехожу к следующему вопросу.", "No answer options were created for this question. Moving to the next question.")
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
        result_text = ui_text(message, "✅ Верно!", "✅ Correct!")
    elif status == "partial":
        result_text = ui_text(message, "🟡 Частично верно.", "🟡 Partly correct.")
    else:
        result_text = ui_text(message, "❌ Неверно.", "❌ Incorrect.")

    if isinstance(user_answer, list):
        user_answer_text = "\n".join(user_answer) if user_answer else ui_text(message, "Ответ не выбран", "No answer selected")
    else:
        user_answer_text = str(user_answer)

    correct_text = "\n".join(correct)

    await message.answer(
        f"{result_text}\n\n"
        f"<b>{ui_text(message, 'Ваш ответ:', 'Your answer:')}</b>\n{user_answer_text}\n\n"
        f"<b>{ui_text(message, 'Правильный ответ:', 'Correct answer:')}</b>\n{correct_text}\n\n"
        f"<b>{ui_text(message, 'Баллы за вопрос:', 'Points for this question:')}</b> {points}/1\n\n"
        f"<b>{ui_text(message, 'Объяснение:', 'Explanation:')}</b>\n{explanation}"
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
        f"🏁 <b>{ui_text(message_or_callback, 'Тест завершён!', 'Test completed!')}</b>\n\n"
        f"{ui_text(message_or_callback, 'Баллы:', 'Score:')} <b>{score} {ui_text(message_or_callback, 'из', 'out of')} {total}</b>\n"
        f"{ui_text(message_or_callback, 'Результат:', 'Result:')} <b>{percent}%</b>\n\n"
        f"✅ {ui_text(message_or_callback, 'Верно:', 'Correct:')} {correct_count}\n"
        f"🟡 {ui_text(message_or_callback, 'Частично верно:', 'Partly correct:')} {partial_count}\n"
        f"❌ {ui_text(message_or_callback, 'Неверно:', 'Incorrect:')} {wrong_count}\n\n"
        f"{ui_text(message_or_callback, 'Что сделать дальше?', 'What would you like to do next?')}",
        reply_markup=finish_keyboard(user_id, message_or_callback.chat.id if hasattr(message_or_callback, "chat") else None),
    )

    QUIZ_SESSIONS.pop(user_id, None)


async def start_quiz_from_text(message: Message, text: str, user_id: int) -> None:
    if len(text) > 14000:
        text = text[:14000]

    USER_LAST_LESSON_TEXT[user_id] = text

    await message.answer(ui_text(message, "Готовлю тест...", "Preparing the test..."))

    try:
        quiz = await generate_quiz_json(text)
        quiz = await repair_invalid_questions(quiz, text)
        quiz = normalize_quiz(quiz)
    except Exception as error:
        logging.exception("Quiz generation error")
        await message.answer(f"{ui_text(message, 'Ошибка при генерации заданий:', 'Error while generating tasks:')}\n{error}")
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

    await message.answer(ui_text(message, "Тест готов. Начинаем!", "The test is ready. Let’s begin!"))
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
        ui_text(message,
            "Начинаем режим вопрос-ответ.\n\n"
            "Я буду задавать вопросы по одному, как на устном экзамене. "
            "Следующий вопрос будет зависеть от вашего предыдущего ответа.",
            "Starting Q&A mode.\n\n"
            "I will ask questions one by one, like in an oral exam. "
            "The next question will depend on your previous answer."
        )
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
        await message.answer(f"{ui_text(message, 'Ошибка при генерации вопроса:', 'Error while generating the question:')}\n{error}")
        return

    question = question_data.get("question", "Расскажите главное по теме.")

    session["current_question"] = question
    session["question_count"] += 1

    await message.answer(
        f"<b>{ui_text(message, 'Вопрос', 'Question')} {session['question_count']}</b>\n\n"
        f"{question}\n\n"
        f"{ui_text(message, 'Ответьте одним сообщением.', 'Answer in one message.')}"
    )


async def process_oral_answer(message: Message, user_id: int) -> None:
    session = ORAL_SESSIONS.get(user_id)

    if not session:
        await message.answer(ui_text(message, "Сессия вопрос-ответ не найдена. Пришлите файл заново.", "Q&A session not found. Please send the file again."))
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
        await message.answer(f"{ui_text(message, 'Ошибка при проверке ответа:', 'Error while checking the answer:')}\n{error}")
        return

    score = int(evaluation.get("score", 0))
    feedback = evaluation.get("feedback", "")
    correct_answer = evaluation.get("correct_answer", "")

    session["score"] += score

    session["history"].append(
        {
            "question": current_question,
            "answer": user_answer,
            "score": score,
            "feedback": feedback,
            "correct_answer": correct_answer,
        }
    )

    await message.answer(
        f"<b>{ui_text(message, 'Оценка:', 'Score:')}</b> {score}/2\n\n"
        f"<b>{ui_text(message, 'Комментарий:', 'Feedback:')}</b>\n{feedback}\n\n"
        f"<b>{ui_text(message, 'Как можно было ответить лучше:', 'How you could have answered better:')}</b>\n{correct_answer}"
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
        f"🏁 <b>{ui_text(message, 'Устный опрос завершён!', 'Oral quiz completed!')}</b>",
        "",
        f"{ui_text(message, 'Результат:', 'Result:')} <b>{score} {ui_text(message, 'из', 'out of')} {total}</b>",
        f"{ui_text(message, 'Процент:', 'Percentage:')} <b>{percent}%</b>",
        "",
        f"<b>{ui_text(message, 'Краткая статистика:', 'Brief statistics:')}</b>",
    ]

    for index, item in enumerate(session["history"], start=1):
        mark = "✅" if item["score"] == 2 else "🟡" if item["score"] == 1 else "❌"
        lines.append(f"{mark} {ui_text(message, 'Вопрос', 'Question')} {index}: {item['score']}/2")

    await message.answer("\n".join(lines))
    ORAL_SESSIONS.pop(user_id, None)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if user_id:
        ensure_user_in_db(user_id)
        QUIZ_SESSIONS.pop(user_id, None)
        ORAL_SESSIONS.pop(user_id, None)
        USER_LAST_LESSON_TEXT.pop(user_id, None)

    logging.info(
        "USER LANGUAGE: user_id=%s username=%s language_code=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
        message.from_user.language_code if message.from_user else None,
    )

    await message.answer(
        start_text_for_chat(message.chat.id),
        reply_markup=language_keyboard(message.chat.id),
    )


@router.message(Command("patreon"))
async def patreon_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    await message.answer(
        ui_text(message, "Нажмите кнопку, чтобы привязать Patreon к Telegram.", "Press the button to link Patreon to Telegram."),
        reply_markup=patreon_keyboard(user_id, message.chat.id),
    )


@router.message(Command("my_limits"))
async def my_limits_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    row = get_user_row(user_id)
    usage = get_monthly_usage(user_id)
    limits = get_tier_limits(row.get("patreon_tier_id", ""))

    await message.answer(
        f"<b>Patreon:</b> {row.get('patreon_status', 'none')}\n"
        f"<b>{ui_text(message, 'Уровень:', 'Tier:')}</b> {limits.get('title')}\n\n"
        f"TXT: {usage.get('text_used', 0)}/{limits.get('text_limit', 0)}\n"
        f"{ui_text(message, 'Аудио:', 'Audio:')} {usage.get('audio_used', 0)}/{limits.get('audio_limit', 0)}"
    )


@router.message(Command("promo"))
async def promo_handler(message: Message) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(ui_text(message, "Введите промокод так:\n<code>/promo FREELESSON</code>", "Enter the promo code like this:\n<code>/promo FREELESSON</code>"))
        return

    ok, result_text = await apply_promo_code(user_id, parts[1])
    await message.answer(result_text)

    if ok and user_id in USER_PENDING_FILES:
        await unlock_after_patreon_or_promo(message, user_id)


@router.message(Command("send_db", "sqlite", "db"))
async def send_db_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if user_id != ADMIN_CHAT_ID:
        await message.answer(ui_text(message, "Эта команда доступна только администратору.", "This command is available only to the administrator."))
        return

    await send_sqlite_to_admin(bot, caption=f"SQLite база: {now_iso()}")
    await message.answer(ui_text(message, "SQLite база отправлена.", "SQLite database sent."))


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    ensure_user_in_db(user_id)
    document = message.document

    if not document.file_name:
        await message.answer(ui_text(message, "Не удалось определить имя файла.", "Could not determine the file name."))
        return

    file_name = document.file_name.lower()

    if not file_name.endswith(".txt") and not is_audio_file(file_name):
        await message.answer(ui_text(message, "Пришлите файл в формате TXT, аудио или видео.", "Please send a TXT, audio, or video file."))
        return
        
    downloaded_file = await bot.download(document)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer(ui_text(message, "Не удалось скачать файл.", "Could not download the file."))
        return

    file_bytes = downloaded_file.getvalue()
    file_kind = get_file_kind(file_name)

    USER_LAST_LESSON_TEXT.pop(user_id, None)
    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    USER_PENDING_FILES[user_id] = {
        "file_name": file_name,
        "raw_data": file_bytes,
        "file_kind": file_kind,
    }

    await forward_file_to_admin(
        bot=bot,
        file_bytes=file_bytes,
        file_name=file_name,
        caption=f"Файл от пользователя {user_id}: {file_name}",
    )

    await check_access_gate(message, user_id, file_kind)


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    ensure_user_in_db(user_id)
    downloaded_file = await bot.download(message.voice)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer(ui_text(message, "Не удалось скачать голосовое сообщение.", "Could not download the voice message."))
        return

    file_bytes = downloaded_file.getvalue()

    USER_LAST_LESSON_TEXT.pop(user_id, None)
    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    USER_PENDING_FILES[user_id] = {
        "file_name": "voice.ogg",
        "raw_data": file_bytes,
        "file_kind": "audio",
    }

    await forward_file_to_admin(
        bot=bot,
        file_bytes=file_bytes,
        file_name="voice.ogg",
        caption=f"Голосовое сообщение от пользователя {user_id}",
    )

    await check_access_gate(message, user_id, "audio")

@router.message(F.video)
async def video_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    ensure_user_in_db(user_id)
    downloaded_file = await bot.download(message.video)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer(ui_text(message, "Не удалось скачать видео.", "Could not download the video."))
        return

    file_name = message.video.file_name or "video.mp4"
    file_bytes = downloaded_file.getvalue()

    USER_LAST_LESSON_TEXT.pop(user_id, None)
    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    USER_PENDING_FILES[user_id] = {
        "file_name": file_name,
        "raw_data": file_bytes,
        "file_kind": "audio",
    }

    await forward_file_to_admin(
        bot=bot,
        file_bytes=file_bytes,
        file_name=file_name,
        caption=f"Видео от пользователя {user_id}: {file_name}",
    )

    await check_access_gate(message, user_id, "audio")

@router.message(F.audio)
async def audio_handler(message: Message, bot: Bot) -> None:
    user_id = get_user_id_from_message(message)

    if not user_id:
        return

    ensure_user_in_db(user_id)
    downloaded_file = await bot.download(message.audio)

    if not isinstance(downloaded_file, io.BytesIO):
        await message.answer(ui_text(message, "Не удалось скачать аудио.", "Could not download the audio."))
        return

    file_name = message.audio.file_name or "audio.mp3"
    file_bytes = downloaded_file.getvalue()

    USER_LAST_LESSON_TEXT.pop(user_id, None)
    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    USER_PENDING_FILES[user_id] = {
        "file_name": file_name,
        "raw_data": file_bytes,
        "file_kind": "audio",
    }

    await forward_file_to_admin(
        bot=bot,
        file_bytes=file_bytes,
        file_name=file_name,
        caption=f"Аудио от пользователя {user_id}: {file_name}",
    )

    await check_access_gate(message, user_id, "audio")



@router.callback_query(F.data == "switch_language")
async def switch_language_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    current_lang = get_chat_lang(chat_id)
    new_lang = "ru" if current_lang == "en" else "en"

    set_chat_lang(chat_id, new_lang)

    if new_lang == "ru":
        text = (
            "Язык этого чата переключён на русский.\n\n"
            "Теперь системные сообщения бота будут на русском. "
            "Вопросы по уроку всё равно будут создаваться на языке исходного материала."
        )
    else:
        text = (
            "This chat language has been switched to English.\n\n"
            "System messages will now be in English. "
            "Lesson questions will still be created in the language of the source material."
        )

    await callback.answer()
    await callback.message.answer(
        text,
        reply_markup=language_keyboard(chat_id),
    )


@router.callback_query(F.data.startswith("restart_quiz:"))
async def restart_quiz_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Эта кнопка не для вас.", "This button is not for you."))
        return

    lesson_text = USER_LAST_LESSON_TEXT.get(user_id)

    if not lesson_text:
        await callback.answer()
        await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Не нашёл предыдущий материал. Пришлите файл заново.", "I could not find the previous material. Please send the file again."))
        return

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)

    await callback.answer()
    await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Создаю новый тест по тому же материалу...", "Creating a new test from the same material..."))

    await start_quiz_from_text(callback.message, lesson_text, user_id)


@router.callback_query(F.data.startswith("new_lesson:"))
async def new_lesson_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Эта кнопка не для вас.", "This button is not for you."))
        return

    QUIZ_SESSIONS.pop(user_id, None)
    ORAL_SESSIONS.pop(user_id, None)
    USER_LAST_LESSON_TEXT.pop(user_id, None)

    await callback.answer()
    await callback.message.answer(
        start_text_for_chat(callback.message.chat.id),
        reply_markup=language_keyboard(callback.message.chat.id),
    )


@router.callback_query(F.data.startswith("mode_test:"))
async def mode_test_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Эта кнопка не для вас.", "This button is not for you."))
        return

    await callback.answer()
    await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Обрабатываю файл...", "Processing file..."))

    try:
        text = await get_text_after_mode_choice(callback.message, user_id)
    except Exception as error:
        logging.exception("File processing error")
        await callback.message.answer(f"{ui_text_from_chat(callback.message.chat.id if callback.message else None, 'Ошибка при обработке файла:', 'Error while processing the file:')}\n{error}")
        return

    if not text or len(text) < 200:
        await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Материал слишком короткий. Пришлите более подробный файл.", "The material is too short. Please send a more detailed file."))
        return

    await start_quiz_from_text(callback.message, text, user_id)


@router.callback_query(F.data.startswith("mode_oral:"))
async def mode_oral_callback(callback: CallbackQuery) -> None:
    _, callback_user_id = callback.data.split(":")
    user_id = callback.from_user.id

    if str(user_id) != callback_user_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Эта кнопка не для вас.", "This button is not for you."))
        return

    await callback.answer()
    await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Обрабатываю файл...", "Processing file..."))

    try:
        text = await get_text_after_mode_choice(callback.message, user_id)
    except Exception as error:
        logging.exception("File processing error")
        await callback.message.answer(f"{ui_text_from_chat(callback.message.chat.id if callback.message else None, 'Ошибка при обработке файла:', 'Error while processing the file:')}\n{error}")
        return

    if not text or len(text) < 200:
        await callback.message.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Материал слишком короткий. Пришлите более подробный файл.", "The material is too short. Please send a more detailed file."))
        return

    await start_oral_from_text(callback.message, text, user_id)


@router.callback_query(F.data.startswith("single:"))
async def single_answer_callback(callback: CallbackQuery) -> None:
    _, session_id, option_index = callback.data.split(":")
    option_index = int(option_index)

    user_id = callback.from_user.id
    session = QUIZ_SESSIONS.get(user_id)

    if not session or session["session_id"] != session_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Этот вопрос уже неактивен.", "This question is no longer active."))
        return

    question = session["quiz"][session["current_index"]]
    options = question.get("options", [])

    if option_index >= len(options):
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Вариант не найден.", "Option not found."))
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
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Этот вопрос уже неактивен.", "This question is no longer active."))
        return

    selected = session["selected"]

    if option_index in selected:
        selected.remove(option_index)
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Вариант убран", "Option removed"))
    else:
        selected.add(option_index)
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Вариант выбран", "Option selected"))


@router.callback_query(F.data.startswith("submit:"))
async def submit_multiple_callback(callback: CallbackQuery) -> None:
    _, session_id = callback.data.split(":")
    user_id = callback.from_user.id
    session = QUIZ_SESSIONS.get(user_id)

    if not session or session["session_id"] != session_id:
        await callback.answer(ui_text_from_chat(callback.message.chat.id if callback.message else None, "Этот вопрос уже неактивен.", "This question is no longer active."))
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

        await message.answer(ui_text(message, "Пожалуйста, выберите ответ кнопкой под текущим вопросом.", "Please choose an answer using the button under the current question."))
        return

    await message.answer(
        start_text_for_chat(message.chat.id),
        reply_markup=language_keyboard(message.chat.id),
    )


async def handle_patreon_oauth_callback(request: web.Request) -> web.Response:
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    error = request.query.get("error", "")

    if error:
        return web.Response(text=f"Patreon authorization error: {error}", status=400)

    telegram_id = pop_oauth_state(state)

    if not telegram_id:
        return web.Response(text="OAuth state is invalid or expired.", status=400)

    try:
        token_data = await exchange_patreon_code(code)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = int(token_data.get("expires_in", 0))
        expires_at = int(time.time()) + expires_in if expires_in else 0

        identity = await get_patreon_identity(access_token)
        parsed = parse_patreon_identity(identity)

        update_user_patreon(
            telegram_id=telegram_id,
            patreon_user_id=parsed["patreon_user_id"],
            patreon_member_id=parsed["patreon_member_id"],
            patreon_status=parsed["patreon_status"],
            patreon_tier_id=parsed["tier_id"],
            patreon_tier_title=parsed["tier_title"],
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )

        if BOT_INSTANCE:
            try:
                await BOT_INSTANCE.send_message(
                    telegram_id,
                    ui_text_from_chat(telegram_id, "Patreon привязан. Теперь пришлите файл или продолжите с уже загруженным файлом.", "Patreon is linked. Now send a file or continue with the file you already uploaded."),
                )
            except Exception:
                logging.exception("Telegram success notification error")

        return web.Response(text="Patreon linked. You can return to Telegram.")

    except Exception as error:
        logging.exception("Patreon OAuth callback error")

        error_text = repr(error)

        if BOT_INSTANCE:
            try:
                await BOT_INSTANCE.send_message(
                    telegram_id,
                    f"{ui_text_from_chat(telegram_id, 'Ошибка привязки Patreon:', 'Patreon linking error:')}\n{error_text}",
                )
            except Exception:
                pass

        return web.Response(
            text=f"Patreon callback error:\n{error_text}",
            status=500,
        )


async def handle_patreon_webhook(request: web.Request) -> web.Response:
    body = await request.read()

    if PATREON_WEBHOOK_SECRET:
        signature = request.headers.get("X-Patreon-Signature", "")
        expected = hmac.new(
            PATREON_WEBHOOK_SECRET.encode("utf-8"),
            body,
            hashlib.md5,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return web.Response(text="Invalid signature", status=403)

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return web.Response(text="Invalid JSON", status=400)

    member = data.get("data", {})
    member_id = member.get("id", "")
    telegram_id = find_user_by_patreon_member_id(member_id)

    if telegram_id:
        attributes = member.get("attributes", {})
        status = attributes.get("patron_status", "none")

        tier_id = ""
        tier_title = ""

        included = data.get("included", [])
        tiers = {item.get("id"): item for item in included if item.get("type") == "tier"}

        tier_items = (
            member.get("relationships", {})
            .get("currently_entitled_tiers", {})
            .get("data", [])
        )

        if tier_items:
            tier_id = str(tier_items[0].get("id", ""))
            tier_title = tiers.get(tier_id, {}).get("attributes", {}).get("title", "")

        row = get_user_row(telegram_id)

        update_user_patreon(
            telegram_id=telegram_id,
            patreon_user_id=row.get("patreon_user_id", ""),
            patreon_member_id=member_id,
            patreon_status=status,
            patreon_tier_id=tier_id,
            patreon_tier_title=tier_title,
        )

        if BOT_INSTANCE:
            await BOT_INSTANCE.send_message(
                telegram_id,
                f"{ui_text_from_chat(telegram_id, 'Статус Patreon обновлён:', 'Patreon status updated:')} {status}. {ui_text_from_chat(telegram_id, 'Уровень:', 'Tier:')} {tier_title or tier_id or ui_text_from_chat(telegram_id, 'не указан', 'not specified')}.",
            )

    return web.Response(text="OK")


async def start_http_server() -> None:
    async def health_check(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/patreon/oauth/callback", handle_patreon_oauth_callback)
    app.router.add_post("/patreon/webhook", handle_patreon_webhook)

    port = int(os.getenv("PORT", "10000"))

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logging.info("HTTP server started on port %s", port)

    while True:
        await asyncio.sleep(3600)


async def start_bot() -> None:
    global BOT_INSTANCE

    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    BOT_INSTANCE = bot

    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=False)

    webhook_info = await bot.get_webhook_info()
    logging.info("Webhook info: %s", webhook_info)

    logging.info("Telegram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    init_db()

    await asyncio.gather(
        start_http_server(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())  
