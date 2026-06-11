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
import datetime
from aiohttp import ClientError, ClientConnectionError, ClientOSError

import db

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
PROXY_URL = os.getenv('PROXY_URL', None)

# Через сколько минут напоминать о неотвеченном сообщении (по умолчанию 10)
REMINDER_MINUTES = int(os.getenv('REMINDER_MINUTES', '10'))
# Как часто планировщик проверяет просроченные треды (сек)
SCHEDULER_INTERVAL = 30
# Дни недели для настроек
DAY_NAMES = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}

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


def _create_bot(use_proxy: bool = True) -> Bot:
    """Создать экземпляр бота с прокси (если указан) или напрямую."""
    if use_proxy and PROXY_URL:
        logger.info(f"🌐 Использование прокси: {PROXY_URL} (таймаут 300с)")
        session = AiohttpSession(proxy=PROXY_URL, timeout=300)
        return Bot(token=TOKEN, session=session, default=_default)
    else:
        if PROXY_URL and not use_proxy:
            logger.warning("⚠️ Прокси недоступен, переключение на прямое соединение")
        else:
            logger.info("🌐 Прямое соединение (без прокси)")
        return Bot(token=TOKEN, default=_default)


bot = _create_bot()

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
        "Команды: /status — статус, /pending — список неотвеченных, "
        "/settings — настройки."
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
        settings = await db.get_settings(user_id)
        await message.answer(
            f"✅ Секретарь подключён\n"
            f"Могу писать от твоего имени: {conn_info.get('can_reply')}\n"
            f"🔔 Задержка: {settings['reminder_delay']} мин\n"
            f"🕐 Активен: {settings['active_time_start']}–{settings['active_time_end']}\n"
            f"📋 Сейчас неотвеченных: <b>{open_count}</b>\n\n"
            f"/settings — изменить настройки"
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


# ──────────────────────────── НАСТРОЙКИ ────────────────────────────

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    user_id = message.from_user.id
    settings = await db.get_settings(user_id)
    await message.answer(
        _format_settings_text(settings),
        reply_markup=_settings_keyboard(settings),
    )


@dp.callback_query(F.data.startswith("sett:"))
async def cb_settings(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    parts = data.split(":")
    action = parts[0]  # "sett"
    cmd = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "close":
            await callback.answer()
            try:
                await callback.message.delete()
            except Exception:
                pass
            return

        if cmd == "main":
            settings = await db.get_settings(user_id)
            await callback.message.edit_text(
                _format_settings_text(settings),
                reply_markup=_settings_keyboard(settings),
            )
            await callback.answer()
            return

        # ── Задержка напоминания ──
        if cmd == "delay":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="5 мин", callback_data="sett:delay_set:5"),
                 InlineKeyboardButton(text="10 мин", callback_data="sett:delay_set:10")],
                [InlineKeyboardButton(text="15 мин", callback_data="sett:delay_set:15"),
                 InlineKeyboardButton(text="30 мин", callback_data="sett:delay_set:30")],
                [InlineKeyboardButton(text="60 мин", callback_data="sett:delay_set:60"),
                 InlineKeyboardButton(text="90 мин", callback_data="sett:delay_set:90")],
                [InlineKeyboardButton(text="120 мин", callback_data="sett:delay_set:120")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="sett:main")],
            ])
            await callback.message.edit_text("⏱ <b>Задержка напоминания</b>\n\nЧерез сколько минут после сообщения напомнить?", reply_markup=kb)
            await callback.answer()
            return

        if cmd == "delay_set":
            minutes = int(parts[2])
            await db.save_settings(user_id, reminder_delay=minutes)
            await callback.answer(f"✅ Задержка: {minutes} мин")
            settings = await db.get_settings(user_id)
            await callback.message.edit_text(
                _format_settings_text(settings),
                reply_markup=_settings_keyboard(settings),
            )
            return

        # ── Дни недели ──
        if cmd == "days":
            settings = await db.get_settings(user_id)
            active_days = {int(d.strip()) for d in settings["active_days"].split(",") if d.strip()}
            rows = []
            for d in range(7):
                label = DAY_NAMES[d]
                checked = "✅ " if d in active_days else "⬜ "
                rows.append([InlineKeyboardButton(
                    text=f"{checked}{label}",
                    callback_data=f"sett:day:{d}",
                )])
            rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="sett:days_done")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await callback.message.edit_text("📅 <b>Рабочие дни</b>\n\nВыбери дни, когда должны приходить напоминания:", reply_markup=kb)
            await callback.answer()
            return

        if cmd == "day":
            day = int(parts[2])
            settings = await db.get_settings(user_id)
            active_days = {int(d.strip()) for d in settings["active_days"].split(",") if d.strip()}
            if day in active_days:
                active_days.discard(day)
            else:
                active_days.add(day)
            days_str = ",".join(str(d) for d in sorted(active_days)) if active_days else ""
            await db.save_settings(user_id, active_days=days_str)
            # Обновляем клавиатуру
            settings = await db.get_settings(user_id)
            rows = []
            for d in range(7):
                label = DAY_NAMES[d]
                checked = "✅ " if d in active_days else "⬜ "
                rows.append([InlineKeyboardButton(
                    text=f"{checked}{label}",
                    callback_data=f"sett:day:{d}",
                )])
            rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="sett:days_done")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            try:
                await callback.message.edit_text("📅 <b>Рабочие дни</b>\n\nВыбери дни, когда должны приходить напоминания:", reply_markup=kb)
            except Exception:
                pass
            await callback.answer()
            return

        if cmd == "days_done":
            await callback.answer("✅ Дни сохранены")
            settings = await db.get_settings(user_id)
            await callback.message.edit_text(
                _format_settings_text(settings),
                reply_markup=_settings_keyboard(settings),
            )
            return

        # ── Макс. количество повторов ──
        if cmd == "maxrem":
            settings = await db.get_settings(user_id)
            val = settings["max_reminders"]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➖", callback_data="sett:maxrem_dec"),
                 InlineKeyboardButton(text=f" {val} ", callback_data="sett:nop"),
                 InlineKeyboardButton(text="➕", callback_data="sett:maxrem_inc")],
                [InlineKeyboardButton(text="✅ Готово", callback_data="sett:main")],
            ])
            await callback.message.edit_text(
                f"🔁 <b>Максимум напоминаний</b>\n\n"
                f"Сколько раз напоминать об одном сообщении (текущее: <b>{val}</b>):",
                reply_markup=kb,
            )
            await callback.answer()
            return

        if cmd == "maxrem_inc":
            settings = await db.get_settings(user_id)
            new_val = min(settings["max_reminders"] + 1, 30)
            await db.save_settings(user_id, max_reminders=new_val)
            await _update_maxrem_message(callback, user_id)
            await callback.answer()
            return

        if cmd == "maxrem_dec":
            settings = await db.get_settings(user_id)
            new_val = max(settings["max_reminders"] - 1, 1)
            await db.save_settings(user_id, max_reminders=new_val)
            await _update_maxrem_message(callback, user_id)
            await callback.answer()
            return

        # ── Выбор времени начала ──
        if cmd == "start_hour":
            await _show_hour_picker(callback, "sett:start_min", "Начало рабочего дня")
            await callback.answer()
            return

        if cmd == "start_min":
            hour = int(parts[2])
            await _show_minute_picker(callback, hour, "active_time_start", "sett:main")
            await callback.answer()
            return

        # ── Выбор времени конца ──
        if cmd == "end_hour":
            await _show_hour_picker(callback, "sett:end_min", "Конец рабочего дня")
            await callback.answer()
            return

        if cmd == "end_min":
            hour = int(parts[2])
            await _show_minute_picker(callback, hour, "active_time_end", "sett:end_hour")
            await callback.answer()
            return

        # ── Установка времени (час+минута известны) ──
        if cmd == "time_set":
            field = parts[2]  # active_time_start или active_time_end
            hour = int(parts[3])
            minute = int(parts[4])
            time_str = f"{hour:02d}:{minute:02d}"
            await db.save_settings(user_id, **{field: time_str})
            await callback.answer(f"✅ Время установлено: {time_str}")
            settings = await db.get_settings(user_id)
            await callback.message.edit_text(
                _format_settings_text(settings),
                reply_markup=_settings_keyboard(settings),
            )
            return

        if cmd == "nop":
            await callback.answer()
            return

        await callback.answer()
    except Exception as e:
        logger.error(f"💥 Ошибка в settings callback: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def _update_maxrem_message(callback: CallbackQuery, user_id: int):
    """Обновить сообщение с выбором макс. повторов."""
    settings = await db.get_settings(user_id)
    val = settings["max_reminders"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➖", callback_data="sett:maxrem_dec"),
         InlineKeyboardButton(text=f" {val} ", callback_data="sett:nop"),
         InlineKeyboardButton(text="➕", callback_data="sett:maxrem_inc")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="sett:main")],
    ])
    try:
        await callback.message.edit_text(
            f"🔁 <b>Максимум напоминаний</b>\n\n"
            f"Сколько раз напоминать об одном сообщении (текущее: <b>{val}</b>):",
            reply_markup=kb,
        )
    except Exception:
        pass


async def _show_hour_picker(callback: CallbackQuery, next_action: str, title: str):
    """Показать выбор часа (0-23)."""
    rows = []
    for h in range(0, 24, 4):
        row = []
        for offset in range(4):
            hour = h + offset
            row.append(InlineKeyboardButton(
                text=f"{hour:02d}",
                callback_data=f"{next_action}:{hour}",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="sett:main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(f"🕐 <b>{title}</b>\n\nВыбери час:", reply_markup=kb)
    except Exception:
        pass


async def _show_minute_picker(callback: CallbackQuery, hour: int, field: str, back_action: str):
    """Показать выбор минут (:00 или :30) для указанного часа."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{hour:02d}:00", callback_data=f"sett:time_set:{field}:{hour}:0"),
         InlineKeyboardButton(text=f"{hour:02d}:30", callback_data=f"sett:time_set:{field}:{hour}:30")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_action)],
    ])
    try:
        await callback.message.edit_text(
            f"🕐 <b>Выбери минуты</b>\n\nЧас: <b>{hour:02d}</b>", reply_markup=kb)
    except Exception:
        pass


@dp.business_connection()
async def handle_business_connection(conn: BusinessConnection):
    user_id = conn.user.id
    rights = getattr(conn, "rights", None)
    if rights is not None:
        can_reply = bool(rights.can_reply)
    else:
        can_reply = bool(getattr(conn, "can_reply", False))
    logger.info(f"BusinessConnection: user={user_id}, enabled={conn.is_enabled}, can_reply={can_reply}")

    user_connections[user_id] = {
        "id": conn.id,
        "is_enabled": conn.is_enabled,
        "can_reply": can_reply,
    }
    await db.upsert_connection(conn.id, user_id, conn.is_enabled, can_reply)

    if conn.is_enabled:
        # Проверяем настройку приватности пересылки
        has_private = await _check_has_private_forwards(user_id)
        settings = await db.get_settings(user_id)
        msg = (
            "✅ Секретарь подключён!\n\n"
            f"Теперь я слежу за входящими. Если ты не ответишь клиенту за "
            f"{settings['reminder_delay']} мин — напомню.\n\n"
            f"⚙️ Настройки: /settings"
        )
        if has_private:
            msg += (
                "\n\n⚠️ <b>Внимание:</b> У вас включена приватность пересылки.\n"
                "Кнопки-ссылки на чаты клиентов без username не будут работать.\n"
                "Отключите в Настройках → Приватность → Пересылка."
            )
        try:
            await bot.send_message(user_id, msg)
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


# ─── Активный период (рабочие часы / дни) ──────────────────────────────

def _parse_time(t_str: str) -> tuple[int, int]:
    """Разобрать HH:MM в (часы, минуты)."""
    parts = t_str.split(":")
    return int(parts[0]), int(parts[1])


def _is_in_active_period(settings: dict, now_dt: datetime.datetime | None = None) -> bool:
    """Проверить, находится ли текущее время в активном периоде."""
    if now_dt is None:
        now_dt = datetime.datetime.now()
    # День недели (0=ПН … 6=ВС)
    active_days_str = settings.get("active_days", "0,1,2,3,4")
    active_days = {int(d.strip()) for d in active_days_str.split(",") if d.strip()}
    if now_dt.weekday() not in active_days:
        return False
    # Время
    cur_min = now_dt.hour * 60 + now_dt.minute
    start_h, start_m = _parse_time(settings.get("active_time_start", "09:00"))
    end_h, end_m = _parse_time(settings.get("active_time_end", "18:00"))
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    return start_min <= cur_min < end_min


def _get_next_active_start(settings: dict) -> int:
    """Unix-время начала следующего активного периода."""
    now = datetime.datetime.now()
    start_h, start_m = _parse_time(settings.get("active_time_start", "09:00"))
    active_days_str = settings.get("active_days", "1,2,3,4,5")
    active_days = {int(d.strip()) for d in active_days_str.split(",") if d.strip()}

    cur_minutes = now.hour * 60 + now.minute
    today_start_min = start_h * 60 + start_m

    # Если сегодня активный день и время ещё не наступило
    if now.weekday() in active_days and cur_minutes < today_start_min:
        cand = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        return int(cand.timestamp())

    # Ищем ближайший активный день
    for offset in range(1, 8):
        nxt = now + datetime.timedelta(days=offset)
        if nxt.weekday() in active_days:
            cand = nxt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            return int(cand.timestamp())

    # Запасной вариант — завтра 9:00
    fallback = (now + datetime.timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return int(fallback.timestamp())


def _format_settings_text(settings: dict) -> str:
    """Форматировать текст с текущими настройками."""
    days_list = [int(d.strip()) for d in settings["active_days"].split(",") if d.strip()]
    days_str = " ".join(DAY_NAMES.get(d, "?") for d in sorted(days_list)) or "Нет"
    return (
        f"⚙️ <b>Настройки secretary</b>\n\n"
        f"🕐 <b>Начало:</b> {settings['active_time_start']}\n"
        f"🕐 <b>Конец:</b> {settings['active_time_end']}\n"
        f"📅 <b>Рабочие дни:</b> {days_str}\n"
        f"🔁 <b>Макс. напоминаний:</b> {settings['max_reminders']}\n"
        f"🔔 <b>Задержка напоминания:</b> {settings['reminder_delay']} мин\n\n"
        f"<i>В нерабочее время напоминания не приходят.\n"
        f"При начале активного периода придёт сводка пропущенных.</i>"
    )


def _settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Клавиатура главной страницы настроек."""
    kb = [
        [InlineKeyboardButton(text=f"🕐 Начало: {settings['active_time_start']}",
                               callback_data="sett:start_hour")],
        [InlineKeyboardButton(text=f"🕐 Конец: {settings['active_time_end']}",
                               callback_data="sett:end_hour")],
        [InlineKeyboardButton(text="📅 Дни недели", callback_data="sett:days")],
        [InlineKeyboardButton(text=f"🔁 Повторов: {settings['max_reminders']}",
                               callback_data="sett:maxrem")],
        [InlineKeyboardButton(text=f"🔔 Задержка: {settings['reminder_delay']} мин",
                               callback_data="sett:delay")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="sett:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ─── Приватность пересылки (has_private_forwards) ─────────────────────
# Кэш: owner_id -> bool (True = включена приватность, URL-ID кнопки не работают)
_has_private_forwards_cache: dict[int, bool] = {}
_private_cache_time: dict[int, float] = {}
_PRIVATE_CACHE_TTL = 3600  # 1 час


async def _check_has_private_forwards(owner_id: int) -> bool:
    """Проверить has_private_forwards у владельца через getChat.

    Если True — URL-ID кнопки (tg://user?id=...) не будут работать.
    Результат кэшируется на 1 час.
    """
    now = time.time()
    cached = _has_private_forwards_cache.get(owner_id)
    cached_at = _private_cache_time.get(owner_id, 0)
    if cached is not None and (now - cached_at) < _PRIVATE_CACHE_TTL:
        return cached
    try:
        chat = await bot.get_chat(owner_id)
        has_private = getattr(chat, "has_private_forwards", False)
        _has_private_forwards_cache[owner_id] = has_private
        _private_cache_time[owner_id] = now
        if has_private:
            logger.info(f"👤 Владелец {owner_id} имеет has_private_forwards=True — URL-ID кнопки недоступны")
        return has_private
    except Exception as e:
        logger.warning(f"Не удалось проверить has_private_forwards для {owner_id}: {e}")
        return False


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
        is_from_owner = sender_id == owner_id

        if is_from_owner:
            # Владелец ответил клиенту в этом чате → закрываем тред.
            closed = await db.close_thread(biz_conn_id, chat_id)
            if closed:
                logger.info(f"✅ Владелец ответил в чат {chat_id} — тред закрыт")
            return

        # Входящее сообщение от клиента
        settings = await db.get_settings(owner_id)
        delay_sec = settings["reminder_delay"] * 60

        # Проверяем, активный ли сейчас период
        if _is_in_active_period(settings):
            due_at = int(time.time()) + delay_sec
            logger.info(
                f"📥 Входящее от {_client_display_name(message)} (chat={chat_id}). "
                f"Напомню через {settings['reminder_delay']} мин."
            )
        else:
            # Неактивный период — откладываем на начало следующего активного.
            # Сообщение уже прождало, поэтому напоминаем сразу в начале периода,
            # без дополнительной задержки.
            next_start = _get_next_active_start(settings)
            due_at = next_start
            logger.info(
                f"📥 Входящее от {_client_display_name(message)} (chat={chat_id}) "
                f"в неактивный период. Дайджест в начале следующего активного окна."
            )

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

        # Если неактивный период — добавляем в дайджест
        if not _is_in_active_period(settings):
            thread = await db.get_thread_by_chat(biz_conn_id, chat_id)
            await db.add_digest_item(
                owner_user_id=owner_id,
                thread_id=thread["id"] if thread else 0,
                client_name=_client_display_name(message),
                client_username=message.from_user.username if message.from_user else None,
                preview=_message_preview(message)[:200],
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
            "Команды: /status, /pending, /settings"
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


def _reminder_keyboard(thread: dict, has_private_forwards: bool = False) -> InlineKeyboardMarkup:
    thread_id = thread["id"]
    rows = []

    # Кнопка «Открыть чат»: URL-ID (tg://user?id=...) не работает при has_private_forwards=True
    username = thread.get("client_username")
    can_use_url_id = username is not None or not has_private_forwards
    if can_use_url_id:
        rows.append([InlineKeyboardButton(text="💬 Открыть чат", url=_chat_link(thread))])

    rows.append([InlineKeyboardButton(text="✅ Готово (ответил)", callback_data=f"done:{thread_id}")])
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


async def _send_reminder(thread: dict, settings: dict | None = None):
    owner_id = thread["owner_user_id"]
    if settings is None:
        settings = await db.get_settings(owner_id)
    max_reminders = settings["max_reminders"]
    repeat_delay = settings["reminder_delay"] * 60

    has_private = await _check_has_private_forwards(owner_id)
    text = _reminder_text(thread)

    # Если у владельца включена приватность и у клиента нет username —
    # URL-ID кнопка не сработает, предупреждаем в тексте
    if has_private and not thread.get("client_username"):
        text += (
            "\n\n⚠️ <i>Кнопка чата недоступна: у вас включена приватность пересылки.\n"
            "Отключите в Настройках → Приватность → Пересылка.</i>"
        )

    try:
        await bot.send_message(
            chat_id=owner_id,
            text=text,
            reply_markup=_reminder_keyboard(thread, has_private_forwards=has_private),
            disable_web_page_preview=True,
        )
        # Планируем следующий повтор, если лимит не исчерпан
        if thread.get("reminder_count", 0) + 1 < max_reminders:
            next_due = int(time.time()) + repeat_delay
            await db.reschedule_thread(thread["id"], next_due, increment_count=True)
        else:
            # Лимит повторов — больше не дёргаем, оставляем открытым без due.
            await db.reschedule_thread(thread["id"], None, increment_count=True)
        logger.info(f"🔔 Напоминание отправлено владельцу {owner_id} (thread {thread['id']})")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить напоминание (thread {thread['id']}): {e}")


async def _send_digest(owner_id: int):
    """Отправить сводку сообщений, полученных в неактивный период."""
    items = await db.get_digest(owner_id)
    if not items:
        return

    # Оставляем только те, что ещё открыты
    open_items = []
    for item in items:
        if item["thread_id"]:
            thread = await db.get_thread(item["thread_id"])
            if thread and thread["status"] == "open":
                open_items.append(item)
        else:
            open_items.append(item)

    if not open_items:
        await db.clear_digest(owner_id)
        return

    lines = ["📋 <b>Пропущенные сообщения</b> (пока вас не было):\n"]
    for item in open_items[:20]:
        name = item.get("client_name") or "Клиент"
        preview = (item.get("preview") or "").strip()
        if len(preview) > 100:
            preview = preview[:100] + "…"
        lines.append(f"• <b>{name}</b> — «{preview}»")

    if len(open_items) > 20:
        lines.append(f"\n... и ещё {len(open_items) - 20} сообщений")

    lines.append("\n\n<i>Напоминания по каждому начнут приходить с задержкой, "
                 "установленной в /settings</i>")

    try:
        await bot.send_message(owner_id, "\n".join(lines))
        logger.info(f"📋 Дайджест отправлен владельцу {owner_id} ({len(open_items)} шт.)")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить дайджест {owner_id}: {e}")

    await db.clear_digest(owner_id)


# Кэш состояния активности пользователей: owner_id -> bool (был ли в активном периоде на прошлой проверке)
_last_active_state: dict[int, bool] = {}


async def reminder_scheduler():
    """Фоновый цикл: раз в SCHEDULER_INTERVAL шлёт просроченные напоминания
    с учётом активных периодов и дайджестов."""
    logger.info(f"⏰ Планировщик напоминаний запущен (интервал {SCHEDULER_INTERVAL}с)")
    while True:
        try:
            due = await db.get_due_threads()
            # Группируем по владельцам
            owner_threads: dict[int, list[dict]] = {}
            for t in due:
                owner_threads.setdefault(t["owner_user_id"], []).append(t)

            for owner_id, threads in owner_threads.items():
                settings = await db.get_settings(owner_id)
                is_active = _is_in_active_period(settings)
                was_active = _last_active_state.get(owner_id)

                if was_active is None:
                    # Первая проверка — просто запоминаем состояние
                    _last_active_state[owner_id] = is_active
                    continue

                # Переход из неактивного в активный → шлём дайджест
                if not was_active and is_active:
                    logger.info(f"➡️ Владелец {owner_id} перешёл в активный период")
                    await _send_digest(owner_id)

                _last_active_state[owner_id] = is_active

                if not is_active:
                    continue  # Неактивный период — пропускаем напоминания

                # Активный период — отправляем просроченные напоминания
                for thread in threads:
                    await _send_reminder(thread, settings)

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
    global bot  # может быть пересоздан при падении прокси
    max_retries = 5
    retry_count = 0
    retry_delay = 2
    using_proxy = PROXY_URL is not None
    proxy_failures = 0

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

            # Fallback: если прокси падает дважды — переключаемся на прямое соединение
            if using_proxy and isinstance(e, (ClientConnectionError, asyncio.TimeoutError)):
                proxy_failures += 1
                if proxy_failures >= 2:
                    logger.warning("⚠️ Прокси недоступен, переключаюсь на прямое соединение")
                    await bot.session.close()
                    bot = _create_bot(use_proxy=False)
                    using_proxy = False

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