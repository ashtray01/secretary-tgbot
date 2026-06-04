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
