"""Хранилище состояния secretary-бота на SQLite (через aiosqlite).

Логика:
- Каждое входящее сообщение клиента создаёт/обновляет "тред" (открытое
  обязательство ответить). Тред привязан к (business_connection_id, chat_id).
- Когда владелец отвечает клиенту в этом чате — тред закрывается.
- Пока тред открыт, для него запланировано напоминание (due_at).
- Планировщик выбирает треды, у которых due_at наступил, и шлёт напоминание
  владельцу. Snooze просто сдвигает due_at.
"""

import time
import aiosqlite

DB_PATH = "secretary.db"


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS connections (
    business_connection_id TEXT PRIMARY KEY,
    owner_user_id          INTEGER NOT NULL,
    is_enabled             INTEGER NOT NULL DEFAULT 1,
    can_reply              INTEGER NOT NULL DEFAULT 0,
    updated_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    business_connection_id TEXT    NOT NULL,
    owner_user_id          INTEGER NOT NULL,
    chat_id                INTEGER NOT NULL,
    client_name            TEXT,
    client_username        TEXT,
    last_message_text      TEXT,
    last_message_id        INTEGER,
    last_in_at             INTEGER NOT NULL,   -- время последнего входящего
    due_at                 INTEGER,            -- когда напомнить (NULL = закрыт)
    reminder_count         INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'open', -- open | answered
    UNIQUE(business_connection_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_due
    ON threads(status, due_at);

CREATE TABLE IF NOT EXISTS user_settings (
    owner_user_id          INTEGER PRIMARY KEY,
    reminder_delay         INTEGER NOT NULL DEFAULT 10,
    active_time_start      TEXT    NOT NULL DEFAULT '09:00',
    active_time_end        TEXT    NOT NULL DEFAULT '21:00',
    active_days            TEXT    NOT NULL DEFAULT '1,2,3,4,5',
    max_reminders          INTEGER NOT NULL DEFAULT 6
);

CREATE TABLE IF NOT EXISTS pending_digest (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id          INTEGER NOT NULL,
    thread_id              INTEGER NOT NULL,
    client_name            TEXT,
    client_username        TEXT,
    preview                TEXT,
    created_at             INTEGER NOT NULL
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()


async def upsert_connection(business_connection_id, owner_user_id, is_enabled, can_reply):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO connections (business_connection_id, owner_user_id, is_enabled, can_reply, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(business_connection_id) DO UPDATE SET
                owner_user_id=excluded.owner_user_id,
                is_enabled=excluded.is_enabled,
                can_reply=excluded.can_reply,
                updated_at=excluded.updated_at
            """,
            (business_connection_id, owner_user_id, int(is_enabled), int(can_reply), int(time.time())),
        )
        await db.commit()


async def get_owner(business_connection_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT owner_user_id FROM connections WHERE business_connection_id=?",
            (business_connection_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_connection_by_owner(owner_user_id):
    """Вернуть первое активное подключение владельца (или None)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM connections WHERE owner_user_id=? AND is_enabled=1 LIMIT 1",
            (owner_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def open_or_update_thread(
    business_connection_id,
    owner_user_id,
    chat_id,
    client_name,
    client_username,
    last_message_text,
    last_message_id,
    due_at,
):
    """Клиент написал — открываем/обновляем тред и переставляем due_at."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO threads (
                business_connection_id, owner_user_id, chat_id,
                client_name, client_username, last_message_text, last_message_id,
                last_in_at, due_at, reminder_count, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'open')
            ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET
                client_name=excluded.client_name,
                client_username=excluded.client_username,
                last_message_text=excluded.last_message_text,
                last_message_id=excluded.last_message_id,
                last_in_at=excluded.last_in_at,
                due_at=excluded.due_at,
                reminder_count=0,
                status='open'
            """,
            (
                business_connection_id, owner_user_id, chat_id,
                client_name, client_username, last_message_text, last_message_id,
                now, due_at,
            ),
        )
        await db.commit()


async def close_thread(business_connection_id, chat_id):
    """Владелец ответил клиенту — закрываем тред."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE threads SET status='answered', due_at=NULL "
            "WHERE business_connection_id=? AND chat_id=? AND status='open'",
            (business_connection_id, chat_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_due_threads(now=None):
    """Открытые треды, у которых наступило время напоминания."""
    now = now if now is not None else int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads "
            "WHERE status='open' AND due_at IS NOT NULL AND due_at <= ? "
            "ORDER BY due_at ASC",
            (now,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_thread(thread_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_thread_by_chat(business_connection_id, chat_id):
    """Найти открытый тред по бизнес-подключению и чату."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads WHERE business_connection_id=? AND chat_id=? AND status='open'",
            (business_connection_id, chat_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def reschedule_thread(thread_id, due_at, increment_count=True):
    """Сдвинуть напоминание (snooze или повтор)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if increment_count:
            await db.execute(
                "UPDATE threads SET due_at=?, reminder_count=reminder_count+1 WHERE id=?",
                (due_at, thread_id),
            )
        else:
            await db.execute(
                "UPDATE threads SET due_at=? WHERE id=?",
                (due_at, thread_id),
            )
        await db.commit()


async def mark_done(thread_id):
    """Владелец нажал «Готово» — закрываем тред вручную."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads SET status='answered', due_at=NULL WHERE id=?",
            (thread_id,),
        )
        await db.commit()


async def count_open(owner_user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM threads WHERE owner_user_id=? AND status='open'",
            (owner_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ──────────────────────── НАСТРОЙКИ ПОЛЬЗОВАТЕЛЕЙ ────────────────────────


async def get_settings(owner_user_id: int) -> dict:
    """Получить настройки пользователя. Если нет — создать с дефолтами."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_settings WHERE owner_user_id=?",
            (owner_user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
    # Нет записи — создаём
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (owner_user_id) VALUES (?)",
            (owner_user_id,),
        )
        await db.commit()
    return {
        "owner_user_id": owner_user_id,
        "reminder_delay": 10,
        "active_time_start": "09:00",
        "active_time_end": "21:00",
        "active_days": "1,2,3,4,5",
        "max_reminders": 6,
    }


async def save_settings(owner_user_id: int, **kwargs):
    """Обновить указанные поля настроек пользователя."""
    allowed = {"reminder_delay", "active_time_start", "active_time_end",
               "active_days", "max_reminders"}
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(owner_user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE user_settings SET {', '.join(sets)} WHERE owner_user_id=?",
            vals,
        )
        await db.commit()


async def get_active_users() -> list[int]:
    """Вернуть список owner_user_id, у которых есть настройки."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT owner_user_id FROM user_settings",
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


# ──────────────────────── ДАЙДЖЕСТ ────────────────────────


async def add_digest_item(owner_user_id: int, thread_id: int,
                          client_name: str, client_username: str,
                          preview: str):
    """Добавить тред в дайджест — сообщение, полученное в неактивный период."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pending_digest "
            "(owner_user_id, thread_id, client_name, client_username, preview, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (owner_user_id, thread_id, client_name, client_username,
             preview, int(time.time())),
        )
        await db.commit()


async def get_digest(owner_user_id: int) -> list[dict]:
    """Получить все элементы дайджеста пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_digest WHERE owner_user_id=? ORDER BY created_at ASC",
            (owner_user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def clear_digest(owner_user_id: int):
    """Очистить дайджест пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_digest WHERE owner_user_id=?",
                         (owner_user_id,))
        await db.commit()
