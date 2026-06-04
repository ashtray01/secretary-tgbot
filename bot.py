import logging
import logging.handlers
import time
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import os
import asyncio
from aiohttp import ClientError, ClientConnectionError, ClientOSError

import db

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
PROXY_URL = os.getenv('PROXY_URL', None)

# Через сколько минут напоминать о неотвеченном сообщении (по умолчанию 10)
REMINDER_MINUTES = int(os.getenv('REMINDER_MINUTES', '10'))
REMINDER_DELAY = REMINDER_MINUTES * 60
# Как часто планировщик проверяет просроченные треды (сек)
SCHEDULER_INTERVAL = 30
# Через сколько повторять напоминание, если владелец так и не ответил (сек)
REPEAT_DELAY = REMINDER_DELAY
# Максимум авто-повторов напоминания на один тред
MAX_REMINDERS = 6

# Варианты snooze: подпись -> минуты
SNOOZE_OPTIONS = [
    ("⏱ 10 мин", 10),
    ("🕐 30 мин", 30),
    ("🕑 1 час", 60),
    ("🌆 3 часа", 180),
]

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in environment variables")

# ── Настройка логирования ──────────────────────────────────────────
# Stderr (journald) — краткий формат
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
)

# Файловое логирование с ротацией
LOG_DIR = os.getenv('LOG_DIR', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, 'secretary-bot.log')

file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding='utf-8',
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(
    logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
)

# Добавляем handler ко всем логгерам через root
root_logger = logging.getLogger()
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Отключаем DEBUG логирование для aiohttp
logging.getLogger('aiohttp').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)

# Инициализация бота с опциональной поддержкой прокси
_default = DefaultBotProperties(parse_mode=ParseMode.HTML)
if PROXY_URL:
    logger.info(f"🌐 Использование прокси: {PROXY_URL}")
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=TOKEN, session=session, default=_default)
else:
    logger.info("🌐 Прямое соединение (без прокси)")
    bot = Bot(token=TOKEN, default=_default)

dp = Dispatcher()

# Кэш подключений в памяти: owner_user_id -> info. Источник истины — БД.
user_connections = {}


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я твой secretary‑бот 🤝\n\n"
        "Моя задача — следить, чтобы ни одно сообщение клиента не осталось без ответа.\n\n"
        "Как это работает:\n"
        f"• Когда клиент пишет тебе в личку — я засекаю таймер на {REMINDER_MINUTES} мин.\n"
        "• Если ты ответил клиенту — я молчу, всё в порядке.\n"
        "• Если не ответил вовремя — я пришлю напоминание с кнопкой-ссылкой на чат "
        "и вариантами «отложить» (snooze).\n\n"
        "Настройка:\n"
        "1. Telegram → Настройки → Telegram Business → Чат-боты → выбери этого бота.\n"
        "2. Дай право читать и отправлять сообщения.\n"
        "3. Готово!\n\n"
        "Команды: /status — статус, /pending — список неотвеченных."
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    conn_info = user_connections.get(user_id)
    # Если в кэше нет — пробуем загрузить из БД
    if conn_info is None:
        db_conn = await db.get_connection_by_owner(user_id)
        if db_conn:
            conn_info = {
                "is_enabled": bool(db_conn["is_enabled"]),
                "can_reply": bool(db_conn["can_reply"]),
            }
            # восстанавливаем кэш
            user_connections[user_id] = conn_info
    if conn_info and conn_info.get("is_enabled"):
        open_count = await db.count_open(user_id)
        await message.answer(
            f"✅ Секретарь подключён\n"
            f"Могу писать от твоего имени: {conn_info.get('can_reply')}\n"
            f"Интервал напоминания: {REMINDER_MINUTES} мин\n"
            f"📋 Сейчас неотвеченных: <b>{open_count}</b>"
        )
    else:
        await message.answer(
            "❌ Секретарь не подключён.\n"
            "Подключи бота в Telegram Business → Чат-боты."
        )


@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    user_id = message.from_user.id
    open_count = await db.count_open(user_id)
    if open_count == 0:
        await message.answer("🎉 Нет неотвеченных сообщений. Отличная работа!")
    else:
        await message.answer(
            f"📋 Неотвеченных сообщений: <b>{open_count}</b>.\n"
            "Я напомню по каждому, как только подойдёт срок."
        )


@dp.business_connection()
async def handle_business_connection(conn: BusinessConnection):
    user_id = conn.user.id
    can_reply = bool(getattr(conn, "can_reply", False) or getattr(conn, "rights", None))
    logger.info(f"BusinessConnection: user={user_id}, enabled={conn.is_enabled}, can_reply={can_reply}")

    user_connections[user_id] = {
        "id": conn.id,
        "is_enabled": conn.is_enabled,
        "can_reply": can_reply,
    }
    await db.upsert_connection(conn.id, user_id, conn.is_enabled, can_reply)

    if conn.is_enabled:
        try:
            await bot.send_message(
                user_id,
                "✅ Секретарь подключён!\n\n"
                f"Теперь я слежу за входящими. Если ты не ответишь клиенту за "
                f"{REMINDER_MINUTES} мин — напомню."
            )
        except Exception as e:
            logger.warning(f"Could not notify: {e}")
    else:
        user_connections.pop(user_id, None)
        try:
            await bot.send_message(user_id, "❌ Секретарь отключён")
        except Exception:
            pass


@dp.deleted_business_messages()
async def handle_deleted_messages(msg: BusinessMessagesDeleted):
    logger.info(f"Deleted {len(msg.message_ids)} business messages in connection {msg.business_connection_id}")


def _client_display_name(message: Message) -> str:
    u = message.from_user
    if not u:
        return "Клиент"
    name = " ".join(filter(None, [u.first_name, u.last_name])) or "Клиент"
    return name


def _message_preview(message: Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    ct = getattr(message, "content_type", None)
    type_labels = {
        "photo": "📷 Фото",
        "video": "🎥 Видео",
        "voice": "🎤 Голосовое",
        "audio": "🎵 Аудио",
        "document": "📎 Документ",
        "sticker": "🩷 Стикер",
        "video_note": "⭕ Кружок",
        "location": "📍 Локация",
        "contact": "👤 Контакт",
    }
    return type_labels.get(ct, "📨 Сообщение без текста")


# Обработчик БИЗНЕС-сообщений (Telegram Business / Secretary Mode)
@dp.business_message()
async def handle_business_messages(message: Message):
    """Сообщения из подключённого бизнес-аккаунта.

    Telegram присылает СЮДА И входящие от клиентов, И исходящие от владельца.
    Отличаем их по флагу message.from_user.id == owner_user_id (владелец),
    либо по message.outgoing, если поле доступно.
    """
    try:
        biz_conn_id = message.business_connection_id
        owner_id = await db.get_owner(biz_conn_id)
        if owner_id is None:
            logger.warning(f"⚠️ Неизвестное подключение {biz_conn_id}, пропуск")
            return

        sender_id = message.from_user.id if message.from_user else None
        chat_id = message.chat.id
        # Сообщение исходящее (написал сам владелец), если отправитель == владелец
        # либо чат совпадает с владельцем не считается; ориентируемся на sender.
        is_from_owner = sender_id == owner_id

        if is_from_owner:
            # Владелец ответил клиенту в этом чате → закрываем тред.
            closed = await db.close_thread(biz_conn_id, chat_id)
            if closed:
                logger.info(f"✅ Владелец ответил в чат {chat_id} — тред закрыт")
            return

        # Входящее сообщение от клиента → открываем/обновляем тред с таймером.
        due_at = int(time.time()) + REMINDER_DELAY
        await db.open_or_update_thread(
            business_connection_id=biz_conn_id,
            owner_user_id=owner_id,
            chat_id=chat_id,
            client_name=_client_display_name(message),
            client_username=message.from_user.username if message.from_user else None,
            last_message_text=_message_preview(message)[:300],
            last_message_id=message.message_id,
            due_at=due_at,
        )
        logger.info(
            f"📥 Входящее от {_client_display_name(message)} (chat={chat_id}). "
            f"Напомню в {REMINDER_MINUTES} мин, если не ответишь."
        )
    except Exception as e:
        logger.error(f"💥 Ошибка в обработчике бизнес-сообщений: {e}", exc_info=True)


# Редактирование бизнес-сообщений: если владелец отредактировал свой ответ,
# тоже считаем чат отвеченным.
@dp.edited_business_message()
async def handle_edited_business_messages(message: Message):
    try:
        biz_conn_id = message.business_connection_id
        owner_id = await db.get_owner(biz_conn_id)
        if owner_id is None:
            return
        sender_id = message.from_user.id if message.from_user else None
        if sender_id == owner_id:
            await db.close_thread(biz_conn_id, message.chat.id)
    except Exception as e:
        logger.error(f"💥 Ошибка в обработчике редактирования: {e}", exc_info=True)


# Обычные сообщения боту в личку (не из бизнес-аккаунта)
@dp.message()
async def handle_all_messages(message: Message):
    try:
        if message.business_connection_id:
            return
        if message.text and message.text.startswith('/'):
            return
        await message.answer(
            "Я работаю в фоне и слежу за неотвеченными сообщениями клиентов.\n"
            "Команды: /status, /pending"
        )
    except Exception as e:
        logger.error(f"💥 Ошибка в обработчике: {type(e).__name__}: {e}", exc_info=True)


# ──────────────────────────── НАПОМИНАНИЯ ────────────────────────────

def _chat_link(thread: dict) -> str:
    """Ссылка на чат с клиентом. Если есть username — t.me/username,
    иначе ссылка на пользователя по id (tg://user?id=)."""
    username = thread.get("client_username")
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={thread['chat_id']}"


def _reminder_keyboard(thread: dict) -> InlineKeyboardMarkup:
    thread_id = thread["id"]
    rows = [
        [InlineKeyboardButton(text="💬 Открыть чат", url=_chat_link(thread))],
        [InlineKeyboardButton(text="✅ Готово (ответил)", callback_data=f"done:{thread_id}")],
    ]
    # Кнопки snooze в две колонки
    snooze_row = []
    for label, minutes in SNOOZE_OPTIONS:
        snooze_row.append(
            InlineKeyboardButton(text=label, callback_data=f"snooze:{thread_id}:{minutes}")
        )
        if len(snooze_row) == 2:
            rows.append(snooze_row)
            snooze_row = []
    if snooze_row:
        rows.append(snooze_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reminder_text(thread: dict) -> str:
    name = thread.get("client_name") or "Клиент"
    username = thread.get("client_username")
    handle = f" (@{username})" if username else ""
    preview = (thread.get("last_message_text") or "").strip()
    if len(preview) > 200:
        preview = preview[:200] + "…"
    waited = max(0, int(time.time()) - int(thread["last_in_at"]))
    waited_min = waited // 60
    count = thread.get("reminder_count", 0)
    header = "🔔 Напоминание" if count == 0 else f"🔁 Повторное напоминание (#{count + 1})"
    return (
        f"{header}\n\n"
        f"Без ответа от <b>{name}</b>{handle}\n"
        f"⏳ Ждёт ответа: ~{waited_min} мин\n\n"
        f"<i>«{preview}»</i>"
    )


async def _send_reminder(thread: dict):
    owner_id = thread["owner_user_id"]
    try:
        await bot.send_message(
            chat_id=owner_id,
            text=_reminder_text(thread),
            reply_markup=_reminder_keyboard(thread),
            disable_web_page_preview=True,
        )
        # Планируем следующий повтор, если лимит не исчерпан.
        if thread.get("reminder_count", 0) + 1 < MAX_REMINDERS:
            next_due = int(time.time()) + REPEAT_DELAY
            await db.reschedule_thread(thread["id"], next_due, increment_count=True)
        else:
            # Лимит повторов — больше не дёргаем, оставляем открытым без due.
            await db.reschedule_thread(thread["id"], None, increment_count=True)
        logger.info(f"🔔 Напоминание отправлено владельцу {owner_id} (thread {thread['id']})")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить напоминание (thread {thread['id']}): {e}")


async def reminder_scheduler():
    """Фоновый цикл: раз в SCHEDULER_INTERVAL шлёт просроченные напоминания."""
    logger.info(f"⏰ Планировщик напоминаний запущен (интервал {SCHEDULER_INTERVAL}с)")
    while True:
        try:
            due = await db.get_due_threads()
            for thread in due:
                await _send_reminder(thread)
        except Exception as e:
            logger.error(f"💥 Ошибка планировщика: {e}", exc_info=True)
        await asyncio.sleep(SCHEDULER_INTERVAL)


# ─────────────────────── ОБРАБОТЧИКИ КНОПОК ───────────────────────

@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    try:
        thread_id = int(callback.data.split(":")[1])
        await db.mark_done(thread_id)
        await callback.answer("Отмечено как отвеченное ✅", show_alert=False)
        try:
            await callback.message.edit_text(
                callback.message.html_text + "\n\n✅ <b>Закрыто вручную</b>",
                reply_markup=None,
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"💥 Ошибка cb_done: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(callback: CallbackQuery):
    try:
        _, thread_id_str, minutes_str = callback.data.split(":")
        thread_id = int(thread_id_str)
        minutes = int(minutes_str)
        thread = await db.get_thread(thread_id)
        if not thread or thread.get("status") != "open":
            await callback.answer("Этот тред уже закрыт.", show_alert=False)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        next_due = int(time.time()) + minutes * 60
        # Snooze не увеличивает счётчик «повторов» как наказание — просто сдвиг.
        await db.reschedule_thread(thread_id, next_due, increment_count=False)
        await callback.answer(f"Напомню через {minutes} мин ⏰")
        try:
            await callback.message.edit_text(
                callback.message.html_text + f"\n\n😴 <b>Отложено на {minutes} мин</b>",
                reply_markup=None,
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"💥 Ошибка cb_snooze: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def main():
    max_retries = 5
    retry_count = 0
    retry_delay = 2
    
    await db.init_db()
    logger.info("🗄️ База данных инициализирована")

    scheduler_task = None

    while retry_count < max_retries:
        try:
            logger.info("🔗 Попытка подключения к Telegram API...")
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Вебхук удалён")

            # Получаем информацию о боте
            bot_info = await bot.get_me()
            logger.info(f"✅ Авторизация успешна. Бот: @{bot_info.username}")

            # Запускаем фоновый планировщик напоминаний
            if scheduler_task is None or scheduler_task.done():
                scheduler_task = asyncio.create_task(reminder_scheduler())

            logger.info("🚀 Бот запущен и ждёт сообщения...")
            # ВАЖНО: явно перечисляем нужные типы апдейтов, включая бизнес-события.
            await dp.start_polling(
                bot,
                allowed_updates=[
                    "message",
                    "edited_message",
                    "callback_query",
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                    "deleted_business_messages",
                ],
            )
            break  # Выход из цикла retry при успехе
            
        except (ClientConnectionError, ClientOSError, asyncio.TimeoutError) as e:
            retry_count += 1
            logger.error(f"❌ Ошибка сетевого соединения: {type(e).__name__}: {e}")
            logger.warning(f"⏳ Попытка {retry_count}/{max_retries}. Переподключение через {retry_delay}с...")
            
            if retry_count < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
            else:
                logger.critical("💀 Исчерпаны все попытки переподключения")
                raise
                
        except ClientError as e:
            retry_count += 1
            logger.error(f"❌ Ошибка HTTP клиента: {e}")
            logger.warning(f"⏳ Переподключение через {retry_delay}с...")
            
            if retry_count < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                logger.critical("💀 Критическая ошибка клиента")
                raise
                
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("⏹️ Бот остановлен (Ctrl+C)")
            break

        except Exception as e:
            logger.critical(f"💥 Непредвиденная ошибка: {type(e).__name__}: {e}", exc_info=True)
            raise

    # Корректное завершение: останавливаем планировщик и закрываем сессию.
    if scheduler_task is not None and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("🧹 Закрытие сессии бота...")
    await bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {e}", exc_info=True)
        exit(1)