from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Настройки и служебные функции
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    timezone_name: str
    timezone: ZoneInfo
    work_chat_id: int | None
    admin_ids: frozenset[int]
    allowed_ids: frozenset[int]
    db_path: Path
    portfolio_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or "PASTE_YOUR_TOKEN" in token:
            raise RuntimeError("В файле .env не указан настоящий BOT_TOKEN")

        timezone_name = os.getenv("TIMEZONE", "Europe/Samara").strip() or "Europe/Samara"
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Неизвестный часовой пояс TIMEZONE={timezone_name}") from exc

        work_chat_raw = os.getenv("WORK_CHAT_ID", "").strip()
        work_chat_id = int(work_chat_raw) if work_chat_raw else None

        def parse_ids(raw: str) -> frozenset[int]:
            result: set[int] = set()
            for value in raw.split(","):
                value = value.strip()
                if value:
                    result.add(int(value))
            return frozenset(result)

        admin_ids = parse_ids(os.getenv("ADMIN_IDS", ""))
        allowed_ids = parse_ids(os.getenv("ALLOWED_IDS", ""))
        db_path = Path(os.getenv("DB_PATH", "data/factory_bot.sqlite3")).expanduser()
        portfolio_dir = Path(os.getenv("PORTFOLIO_DIR", "data/portfolio")).expanduser()
        return cls(
            bot_token=token,
            timezone_name=timezone_name,
            timezone=tz,
            work_chat_id=work_chat_id,
            admin_ids=admin_ids,
            allowed_ids=allowed_ids,
            db_path=db_path,
            portfolio_dir=portfolio_dir,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime должен содержать часовой пояс")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_dt_text(value: str | None, tz: ZoneInfo) -> str:
    if not value:
        return "без срока"
    return from_utc_iso(value).astimezone(tz).strftime("%d.%m.%Y %H:%M")


def money_text(value: float | Decimal) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    formatted = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    if formatted.endswith(",00"):
        formatted = formatted[:-3]
    return f"{formatted} ₽"


def parse_amount(text: str) -> Decimal:
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Введите сумму числом, например 4850 или 4850,50") from exc
    if value <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return value.quantize(Decimal("0.01"))


def parse_user_datetime(text: str, tz: ZoneInfo) -> datetime:
    """Поддерживает: сегодня 18:00, завтра 10:30, 18.07.2026 14:00, 18.07 14:00."""
    raw = " ".join(text.lower().strip().split())
    now_local = datetime.now(tz)

    relative = re.fullmatch(r"(сегодня|завтра)\s+(\d{1,2}):(\d{2})", raw)
    if relative:
        day_word, hours, minutes = relative.groups()
        target_date = now_local.date() + timedelta(days=1 if day_word == "завтра" else 0)
        result = datetime.combine(target_date, time(int(hours), int(minutes)), tzinfo=tz)
    else:
        formats = (
            "%d.%m.%Y %H:%M",
            "%d.%m.%y %H:%M",
            "%Y-%m-%d %H:%M",
            "%d.%m %H:%M",
        )
        result = None
        for fmt in formats:
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt == "%d.%m %H:%M":
                    parsed = parsed.replace(year=now_local.year)
                    if parsed.replace(tzinfo=tz) < now_local - timedelta(minutes=1):
                        parsed = parsed.replace(year=now_local.year + 1)
                result = parsed.replace(tzinfo=tz)
                break
            except ValueError:
                continue
        if result is None:
            raise ValueError(
                "Не понял дату. Примеры: <code>завтра 10:00</code> или "
                "<code>18.07.2026 14:30</code>"
            )

    if result <= now_local:
        raise ValueError("Дата и время должны быть в будущем")
    return result.astimezone(timezone.utc)


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


# ---------------------------------------------------------------------------
# База данных SQLite
# ---------------------------------------------------------------------------


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        return db

    async def init(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            username TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            client TEXT,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            order_id INTEGER REFERENCES orders(id),
            title TEXT NOT NULL,
            description TEXT,
            assignee_id INTEGER NOT NULL REFERENCES users(id),
            creator_id INTEGER NOT NULL REFERENCES users(id),
            priority TEXT NOT NULL DEFAULT 'normal',
            due_at_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            last_overdue_notice_date TEXT
        );

        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            assignee_id INTEGER NOT NULL REFERENCES users(id),
            creator_id INTEGER NOT NULL REFERENCES users(id),
            priority TEXT NOT NULL DEFAULT 'normal',
            due_at_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            last_overdue_notice_date TEXT
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER REFERENCES orders(id),
            amount TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            receipt_file_id TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            expense_date TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expense_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
            telegram_file_id TEXT NOT NULL,
            telegram_file_unique_id TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(expense_id, telegram_file_id)
        );

        CREATE TABLE IF NOT EXISTS object_recent_views (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            viewed_at_utc TEXT NOT NULL,
            PRIMARY KEY(user_id, order_id)
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            creator_id INTEGER REFERENCES users(id),
            target_chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at_utc TEXT NOT NULL,
            sent_at_utc TEXT,
            cancelled INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS order_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            text TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_by INTEGER,
            deleted_at_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            stage TEXT NOT NULL,
            telegram_file_id TEXT NOT NULL,
            telegram_file_unique_id TEXT,
            local_path TEXT,
            caption TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_by INTEGER,
            deleted_at_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER REFERENCES orders(id),
            amount TEXT NOT NULL,
            payment_type TEXT NOT NULL,
            description TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            income_date TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_by INTEGER,
            deleted_at_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            text TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_by INTEGER,
            deleted_at_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS object_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_by INTEGER NOT NULL REFERENCES users(id),
            changed_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            text TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS task_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            telegram_file_id TEXT NOT NULL,
            caption TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS todo_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id INTEGER NOT NULL REFERENCES todos(id),
            text TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS todo_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id INTEGER NOT NULL REFERENCES todos(id),
            telegram_file_id TEXT NOT NULL,
            caption TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at_utc TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_agreements_order ON agreements(order_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_status_history_order ON object_status_history(order_id, id);
        CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_task_photos_task ON task_photos(task_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_todo_comments_todo ON todo_comments(todo_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_todo_photos_todo ON todo_photos(todo_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at_utc);
        CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(status, due_at_utc);
        CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
        CREATE INDEX IF NOT EXISTS idx_expense_receipts_expense ON expense_receipts(expense_id, id);
        CREATE INDEX IF NOT EXISTS idx_recent_views_user ON object_recent_views(user_id, viewed_at_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(cancelled, sent_at_utc, remind_at_utc);
        CREATE INDEX IF NOT EXISTS idx_income_date ON incomes(deleted, income_date);
        CREATE INDEX IF NOT EXISTS idx_comments_order ON order_comments(order_id, deleted);
        CREATE INDEX IF NOT EXISTS idx_portfolio_order ON portfolio_photos(order_id, stage, deleted);
        """
        db = await self._connect()
        try:
            await db.executescript(schema)
            await self._migrate(db)
            await db.execute(
                """
                INSERT OR IGNORE INTO expense_receipts(expense_id, telegram_file_id, created_at_utc)
                SELECT id, receipt_file_id, created_at_utc
                FROM expenses
                WHERE receipt_file_id IS NOT NULL AND TRIM(receipt_file_id) <> ''
                """
            )
            await db.commit()
        finally:
            await db.close()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        migrations: dict[str, list[tuple[str, str]]] = {
            "users": [
                ("role", "TEXT NOT NULL DEFAULT 'employee'"),
                ("name_confirmed", "INTEGER NOT NULL DEFAULT 1"),
                ("updated_at_utc", "TEXT"),
                ("blocked_at_utc", "TEXT"),
            ],
            "orders": [
                ("category", "TEXT NOT NULL DEFAULT 'Другое'"),
                ("address", "TEXT"),
                ("client_phone", "TEXT"),
                ("client_telegram", "TEXT"),
                ("responsible_id", "INTEGER"),
                ("due_at_utc", "TEXT"),
                ("object_status", "TEXT NOT NULL DEFAULT 'new'"),
                ("updated_at_utc", "TEXT"),
            ],
            "todos": [
                ("category", "TEXT NOT NULL DEFAULT 'Прочее'"),
                ("description", "TEXT"),
                ("repeat_rule", "TEXT NOT NULL DEFAULT 'none'"),
                ("has_due", "INTEGER NOT NULL DEFAULT 1"),
            ],
            "expenses": [
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("deleted_by", "INTEGER"),
                ("deleted_at_utc", "TEXT"),
                ("todo_id", "INTEGER"),
            ],
            "portfolio_photos": [
                ("is_best", "INTEGER NOT NULL DEFAULT 0"),
                ("for_site", "INTEGER NOT NULL DEFAULT 0"),
                ("before_after", "TEXT"),
            ],
            "reminders": [
                ("repeat_rule", "TEXT NOT NULL DEFAULT 'none'"),
            ],
        }
        for table, columns in migrations.items():
            cursor = await db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            for name, definition in columns:
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid
        finally:
            await db.close()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        db = await self._connect()
        try:
            cursor = await db.execute(sql, params)
            return await cursor.fetchone()
        finally:
            await db.close()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        db = await self._connect()
        try:
            cursor = await db.execute(sql, params)
            return list(await cursor.fetchall())
        finally:
            await db.close()

    async def seed_access(self, admin_ids: Sequence[int], allowed_ids: Sequence[int]) -> None:
        now = utc_iso(utc_now())
        for telegram_id in set(allowed_ids) - set(admin_ids):
            await self.execute(
                """
                INSERT INTO users (
                    telegram_id, full_name, active, role, name_confirmed,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, 1, 'employee', 0, ?, ?)
                ON CONFLICT(telegram_id) DO NOTHING
                """,
                (telegram_id, f"Сотрудник {telegram_id}", now, now),
            )
        for telegram_id in set(admin_ids):
            await self.execute(
                """
                INSERT INTO users (
                    telegram_id, full_name, active, role, name_confirmed,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, 1, 'admin', 0, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    active=1, role='admin', blocked_at_utc=NULL
                """,
                (telegram_id, f"Администратор {telegram_id}", now, now),
            )

    async def user_by_telegram_id(self, telegram_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))

    async def is_allowed(self, telegram_id: int) -> bool:
        return await self.fetchone(
            "SELECT 1 FROM users WHERE telegram_id=? AND active=1", (telegram_id,)
        ) is not None

    async def is_admin(self, telegram_id: int) -> bool:
        return await self.fetchone(
            "SELECT 1 FROM users WHERE telegram_id=? AND active=1 AND role='admin'",
            (telegram_id,),
        ) is not None

    async def ensure_user(self, telegram_id: int, full_name: str, username: str | None) -> int:
        row = await self.user_by_telegram_id(telegram_id)
        if row is None or not int(row["active"]):
            raise PermissionError("Пользователь не имеет доступа")
        saved_name = row["full_name"] if int(row["name_confirmed"]) else full_name[:100]
        await self.execute(
            """
            UPDATE users SET full_name=?, username=?, updated_at_utc=?
            WHERE telegram_id=?
            """,
            (saved_name, username, utc_iso(utc_now()), telegram_id),
        )
        return int(row["id"])

    async def rename_user(self, telegram_id: int, full_name: str) -> None:
        await self.execute(
            """
            UPDATE users SET full_name=?, name_confirmed=1, updated_at_utc=?
            WHERE telegram_id=? AND active=1
            """,
            (full_name, utc_iso(utc_now()), telegram_id),
        )

    async def users(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT id, telegram_id, full_name, username, role, name_confirmed
            FROM users WHERE active=1 ORDER BY full_name
            """
        )

    async def staff(self, active: bool | None = None) -> list[aiosqlite.Row]:
        if active is None:
            return await self.fetchall(
                "SELECT * FROM users ORDER BY active DESC, role, full_name"
            )
        return await self.fetchall(
            "SELECT * FROM users WHERE active=? ORDER BY role, full_name",
            (1 if active else 0,),
        )

    async def add_staff(self, telegram_id: int, role: str = "employee") -> int:
        now = utc_iso(utc_now())
        await self.execute(
            """
            INSERT INTO users (
                telegram_id, full_name, active, role, name_confirmed,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, 1, ?, 0, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                active=1, role=?, blocked_at_utc=NULL, updated_at_utc=?
            """,
            (
                telegram_id,
                f"Сотрудник {telegram_id}",
                role,
                now,
                now,
                role,
                now,
            ),
        )
        row = await self.user_by_telegram_id(telegram_id)
        assert row is not None
        return int(row["id"])

    async def set_staff_active(self, telegram_id: int, active: bool) -> None:
        await self.execute(
            """
            UPDATE users SET active=?, blocked_at_utc=?, updated_at_utc=?
            WHERE telegram_id=?
            """,
            (
                1 if active else 0,
                None if active else utc_iso(utc_now()),
                utc_iso(utc_now()),
                telegram_id,
            ),
        )
        if not active:
            await self.execute(
                """
                UPDATE reminders SET cancelled=1
                WHERE target_chat_id=? AND sent_at_utc IS NULL
                """,
                (telegram_id,),
            )

    async def set_staff_role(self, telegram_id: int, role: str) -> None:
        await self.execute(
            "UPDATE users SET role=?, updated_at_utc=? WHERE telegram_id=?",
            (role, utc_iso(utc_now()), telegram_id),
        )

    async def audit(
        self,
        actor_id: int | None,
        action: str,
        entity_type: str,
        entity_id: int | None,
        details: str = "",
    ) -> None:
        await self.execute(
            """
            INSERT INTO audit_log (
                actor_id, action, entity_type, entity_id, details, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (actor_id, action, entity_type, entity_id, details or None, utc_iso(utc_now())),
        )

    async def user_by_id(self, user_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM users WHERE id=?", (user_id,))

    async def create_order(self, title: str, client: str, description: str, creator_id: int) -> int:
        return await self.execute(
            """
            INSERT INTO orders (title, client, description, created_by, created_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title, client or None, description or None, creator_id, utc_iso(utc_now())),
        )

    async def active_orders(self, limit: int = 50) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT o.*, u.full_name AS creator_name,
                   (SELECT COUNT(*) FROM order_comments c
                    WHERE c.order_id=o.id AND c.deleted=0) AS comment_count,
                   (SELECT COUNT(*) FROM portfolio_photos p
                    WHERE p.order_id=o.id AND p.deleted=0) AS photo_count
            FROM orders o JOIN users u ON u.id=o.created_by
            WHERE o.status='active'
            ORDER BY o.id DESC LIMIT ?
            """,
            (limit,),
        )

    async def order_by_id(self, order_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT o.*, u.full_name AS creator_name,
                   (SELECT COUNT(*) FROM order_comments c
                    WHERE c.order_id=o.id AND c.deleted=0) AS comment_count,
                   (SELECT COUNT(*) FROM portfolio_photos p
                    WHERE p.order_id=o.id AND p.deleted=0) AS photo_count
            FROM orders o JOIN users u ON u.id=o.created_by
            WHERE o.id=?
            """,
            (order_id,),
        )

    async def create_task(
        self,
        chat_id: int,
        order_id: int | None,
        title: str,
        description: str,
        assignee_id: int,
        creator_id: int,
        priority: str,
        due_at_utc: datetime,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO tasks (
                chat_id, order_id, title, description, assignee_id, creator_id,
                priority, due_at_utc, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                order_id,
                title,
                description or None,
                assignee_id,
                creator_id,
                priority,
                utc_iso(due_at_utc),
                utc_iso(utc_now()),
            ),
        )

    async def task_by_id(self, task_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT t.*, a.full_name AS assignee_name, a.telegram_id AS assignee_telegram_id,
                   c.full_name AS creator_name, c.telegram_id AS creator_telegram_id,
                   o.title AS order_title
            FROM tasks t
            JOIN users a ON a.id=t.assignee_id
            JOIN users c ON c.id=t.creator_id
            LEFT JOIN orders o ON o.id=t.order_id
            WHERE t.id=?
            """,
            (task_id,),
        )

    async def list_tasks(
        self,
        mode: str,
        telegram_id: int | None = None,
        now_utc: datetime | None = None,
        limit: int = 20,
    ) -> list[aiosqlite.Row]:
        where = ["t.status <> 'done'"]
        params: list[Any] = []
        if mode == "mine":
            where.append("a.telegram_id=?")
            params.append(telegram_id)
        elif mode == "overdue":
            where.append("t.due_at_utc < ?")
            params.append(utc_iso(now_utc or utc_now()))
        params.append(limit)
        return await self.fetchall(
            f"""
            SELECT t.*, a.full_name AS assignee_name, a.telegram_id AS assignee_telegram_id,
                   c.full_name AS creator_name, c.telegram_id AS creator_telegram_id,
                   o.title AS order_title
            FROM tasks t
            JOIN users a ON a.id=t.assignee_id
            JOIN users c ON c.id=t.creator_id
            LEFT JOIN orders o ON o.id=t.order_id
            WHERE {' AND '.join(where)}
            ORDER BY t.due_at_utc ASC LIMIT ?
            """,
            params,
        )

    async def set_task_status(self, task_id: int, status: str) -> None:
        completed = utc_iso(utc_now()) if status == "done" else None
        await self.execute(
            "UPDATE tasks SET status=?, completed_at_utc=? WHERE id=?",
            (status, completed, task_id),
        )
        if status == "done":
            await self.cancel_entity_reminders("task", task_id)

    async def create_todo(
        self,
        chat_id: int,
        title: str,
        assignee_id: int,
        creator_id: int,
        priority: str,
        due_at_utc: datetime,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO todos (
                chat_id, title, assignee_id, creator_id, priority, due_at_utc, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                title,
                assignee_id,
                creator_id,
                priority,
                utc_iso(due_at_utc),
                utc_iso(utc_now()),
            ),
        )

    async def todo_by_id(self, todo_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT t.*, a.full_name AS assignee_name, a.telegram_id AS assignee_telegram_id,
                   c.full_name AS creator_name, c.telegram_id AS creator_telegram_id
            FROM todos t
            JOIN users a ON a.id=t.assignee_id
            JOIN users c ON c.id=t.creator_id
            WHERE t.id=?
            """,
            (todo_id,),
        )

    async def list_todos(
        self,
        mode: str,
        telegram_id: int | None = None,
        now_utc: datetime | None = None,
        limit: int = 20,
    ) -> list[aiosqlite.Row]:
        where = ["t.status <> 'done'"]
        params: list[Any] = []
        if mode == "mine":
            where.append("a.telegram_id=?")
            params.append(telegram_id)
        elif mode == "overdue":
            where.append("t.due_at_utc < ?")
            params.append(utc_iso(now_utc or utc_now()))
        params.append(limit)
        return await self.fetchall(
            f"""
            SELECT t.*, a.full_name AS assignee_name, a.telegram_id AS assignee_telegram_id,
                   c.full_name AS creator_name, c.telegram_id AS creator_telegram_id
            FROM todos t
            JOIN users a ON a.id=t.assignee_id
            JOIN users c ON c.id=t.creator_id
            WHERE {' AND '.join(where)}
            ORDER BY t.due_at_utc ASC LIMIT ?
            """,
            params,
        )

    async def set_todo_status(self, todo_id: int, status: str) -> None:
        completed = utc_iso(utc_now()) if status == "done" else None
        await self.execute(
            "UPDATE todos SET status=?, completed_at_utc=? WHERE id=?",
            (status, completed, todo_id),
        )
        if status == "done":
            await self.cancel_entity_reminders("todo", todo_id)

    async def create_expense(
        self,
        order_id: int | None,
        amount: Decimal,
        category: str,
        description: str,
        receipt_file_id: str | None,
        creator_id: int,
        expense_date: date,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO expenses (
                order_id, amount, category, description, receipt_file_id,
                created_by, expense_date, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                str(amount),
                category,
                description or None,
                receipt_file_id,
                creator_id,
                expense_date.isoformat(),
                utc_iso(utc_now()),
            ),
        )

    async def expense_by_id(self, expense_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT e.*, u.full_name AS creator_name, o.title AS order_title
            FROM expenses e
            JOIN users u ON u.id=e.created_by
            LEFT JOIN orders o ON o.id=e.order_id
            WHERE e.id=?
            """,
            (expense_id,),
        )

    async def recent_expenses(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT e.*, u.full_name AS creator_name, o.title AS order_title
            FROM expenses e
            JOIN users u ON u.id=e.created_by
            LEFT JOIN orders o ON o.id=e.order_id
            WHERE e.deleted=0
            ORDER BY e.id DESC LIMIT ?
            """,
            (limit,),
        )

    async def expense_summary(self, start: date, end: date) -> tuple[Decimal, list[aiosqlite.Row]]:
        rows = await self.fetchall(
            """
            SELECT category, SUM(CAST(amount AS REAL)) AS total
            FROM expenses
            WHERE deleted=0 AND expense_date BETWEEN ? AND ?
            GROUP BY category ORDER BY total DESC
            """,
            (start.isoformat(), end.isoformat()),
        )
        total = sum((Decimal(str(row["total"] or 0)) for row in rows), Decimal("0"))
        return total, rows

    async def order_expense_summary(self, start: date, end: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT COALESCE(o.title, 'Без заказа') AS order_title,
                   SUM(CAST(e.amount AS REAL)) AS total
            FROM expenses e
            LEFT JOIN orders o ON o.id=e.order_id
            WHERE e.deleted=0 AND e.expense_date BETWEEN ? AND ?
            GROUP BY COALESCE(o.title, 'Без заказа')
            ORDER BY total DESC
            """,
            (start.isoformat(), end.isoformat()),
        )

    async def expenses_for_period(self, start: date, end: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT e.id, e.expense_date, e.amount, e.category, e.description,
                   COALESCE(o.title, '') AS order_title, u.full_name AS creator_name,
                   CASE WHEN e.receipt_file_id IS NULL THEN 'нет' ELSE 'да' END AS has_receipt
            FROM expenses e
            JOIN users u ON u.id=e.created_by
            LEFT JOIN orders o ON o.id=e.order_id
            WHERE e.deleted=0 AND e.expense_date BETWEEN ? AND ?
            ORDER BY e.expense_date, e.id
            """,
            (start.isoformat(), end.isoformat()),
        )

    async def add_reminder(
        self,
        entity_type: str,
        entity_id: int | None,
        creator_id: int | None,
        target_chat_id: int,
        text: str,
        remind_at_utc: datetime,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO reminders (
                entity_type, entity_id, creator_id, target_chat_id, text,
                remind_at_utc, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                creator_id,
                target_chat_id,
                text,
                utc_iso(remind_at_utc),
                utc_iso(utc_now()),
            ),
        )

    async def schedule_entity_reminders(
        self,
        entity_type: str,
        entity_id: int,
        creator_id: int,
        target_chat_ids: Sequence[int],
        title: str,
        due_at_utc: datetime,
    ) -> None:
        now = utc_now()
        labels = (
            (timedelta(hours=24), "до срока остались сутки"),
            (timedelta(hours=3), "до срока осталось 3 часа"),
            (timedelta(0), "срок наступил"),
        )
        entity_label = "задача" if entity_type == "task" else "дело"
        for target_chat_id in set(target_chat_ids):
            for offset, label in labels:
                remind_at = due_at_utc - offset
                if remind_at > now:
                    await self.add_reminder(
                        entity_type,
                        entity_id,
                        creator_id,
                        target_chat_id,
                        f"{entity_label.capitalize()} №{entity_id}: {title}. {label}.",
                        remind_at,
                    )

    async def due_reminders(self, now: datetime, limit: int = 100) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT * FROM reminders
            WHERE cancelled=0 AND sent_at_utc IS NULL AND remind_at_utc <= ?
            ORDER BY remind_at_utc LIMIT ?
            """,
            (utc_iso(now), limit),
        )

    async def mark_reminder_sent(self, reminder_id: int) -> None:
        await self.execute(
            "UPDATE reminders SET sent_at_utc=? WHERE id=?",
            (utc_iso(utc_now()), reminder_id),
        )

    async def cancel_reminder(self, reminder_id: int, creator_id: int | None = None) -> bool:
        if creator_id is None:
            changed = await self.execute("UPDATE reminders SET cancelled=1 WHERE id=?", (reminder_id,))
            return changed >= 0
        row = await self.fetchone(
            "SELECT id FROM reminders WHERE id=? AND creator_id=? AND entity_type='custom'",
            (reminder_id, creator_id),
        )
        if not row:
            return False
        await self.execute("UPDATE reminders SET cancelled=1 WHERE id=?", (reminder_id,))
        return True

    async def cancel_entity_reminders(self, entity_type: str, entity_id: int) -> None:
        await self.execute(
            """
            UPDATE reminders SET cancelled=1
            WHERE entity_type=? AND entity_id=? AND sent_at_utc IS NULL
            """,
            (entity_type, entity_id),
        )

    async def custom_reminders(self, creator_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT * FROM reminders
            WHERE entity_type='custom' AND creator_id=? AND cancelled=0 AND sent_at_utc IS NULL
            ORDER BY remind_at_utc LIMIT ?
            """,
            (creator_id, limit),
        )

    async def overdue_tasks(self, now: datetime, local_day: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT t.*, a.telegram_id AS assignee_telegram_id, a.full_name AS assignee_name
            FROM tasks t JOIN users a ON a.id=t.assignee_id
            WHERE t.status <> 'done' AND t.due_at_utc < ?
              AND (t.last_overdue_notice_date IS NULL OR t.last_overdue_notice_date <> ?)
            ORDER BY t.due_at_utc LIMIT 100
            """,
            (utc_iso(now), local_day.isoformat()),
        )

    async def overdue_todos(self, now: datetime, local_day: date) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT t.*, a.telegram_id AS assignee_telegram_id, a.full_name AS assignee_name
            FROM todos t JOIN users a ON a.id=t.assignee_id
            WHERE t.status <> 'done' AND t.due_at_utc < ?
              AND (t.last_overdue_notice_date IS NULL OR t.last_overdue_notice_date <> ?)
            ORDER BY t.due_at_utc LIMIT 100
            """,
            (utc_iso(now), local_day.isoformat()),
        )

    async def mark_overdue_notified(self, entity_type: str, entity_id: int, local_day: date) -> None:
        table = "tasks" if entity_type == "task" else "todos"
        await self.execute(
            f"UPDATE {table} SET last_overdue_notice_date=? WHERE id=?",
            (local_day.isoformat(), entity_id),
        )


# ---------------------------------------------------------------------------
# Клавиатуры и оформление карточек
# ---------------------------------------------------------------------------


BTN_BACK = "⬅️ Главное меню"


def reply_keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        ["📦 Объекты", "✅ Мои задачи"],
        ["📝 Дела мастерской", "⏰ Напоминания"],
        ["💰 Финансы", "📸 Портфолио"],
    ]
    if is_admin:
        rows.append(["👥 Сотрудники", "⚙️ Помощь"])
    else:
        rows.append(["⚙️ Помощь"])
    return reply_keyboard(rows)


def tasks_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        [
            ["➕ Новая задача"],
            ["👤 Мои задачи", "📋 Все задачи"],
            ["⚠️ Просроченные задачи"],
            [BTN_BACK],
        ]
    )


def todos_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        [
            ["➕ Новое дело"],
            ["👤 Мои дела", "📋 Все дела"],
            ["⚠️ Просроченные дела"],
            [BTN_BACK],
        ]
    )


def orders_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard([["➕ Новый заказ"], ["📋 Список заказов"], [BTN_BACK]])


def reminders_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard([
        ["➕ Новое напоминание"],
        ["📋 Активные напоминания", "✅ Выполненные напоминания"],
        ["🔁 Повторяющиеся напоминания"],
        [BTN_BACK],
    ])


def expenses_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [["➕ Добавить расход"], ["🧾 Последние расходы"]]
    if is_admin:
        rows.append(["📤 Выгрузить расходы"])
    rows.append([BTN_BACK])
    return reply_keyboard(rows)


def reports_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        [
            ["📅 Финансы сегодня", "📆 Финансы за месяц"],
            ["🗓 Выбрать период", "📦 Финансы по заказам"],
            ["📤 Выгрузить финансы", "⚠️ Сводка просрочек"],
            [BTN_BACK],
        ]
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard([["Пропустить"], ["❌ Отмена"]])


def orders_inline(orders: Sequence[aiosqlite.Row], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"№{row['id']} — {row['title'][:40]}", callback_data=f"{prefix}:{row['id']}")]
        for row in orders[:30]
    ]
    rows.append([InlineKeyboardButton(text="Без заказа", callback_data=f"{prefix}:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def staff_inline(users: Sequence[aiosqlite.Row], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=row["full_name"][:50], callback_data=f"{prefix}:{row['id']}")]
        for row in users[:50]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def priority_inline(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Обычный", callback_data=f"{prefix}:normal"),
                InlineKeyboardButton(text="🟠 Высокий", callback_data=f"{prefix}:high"),
            ],
            [InlineKeyboardButton(text="🔴 Срочный", callback_data=f"{prefix}:urgent")],
        ]
    )


def reminder_destination_inline(work_chat_available: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👤 Напомнить мне", callback_data="rem_dest:me")]]
    if work_chat_available:
        rows.append([InlineKeyboardButton(text="👥 В рабочий чат", callback_data="rem_dest:work")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_actions(task_id: int, status: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "new":
        rows.append([InlineKeyboardButton(text="▶️ В работу", callback_data=f"task_start:{task_id}")])
    if status != "done":
        rows.append([InlineKeyboardButton(text="✅ Выполнено", callback_data=f"task_done:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def todo_actions(todo_id: int, status: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "new":
        rows.append([InlineKeyboardButton(text="▶️ В работу", callback_data=f"todo_start:{todo_id}")])
    if status != "done":
        rows.append([InlineKeyboardButton(text="✅ Выполнено", callback_data=f"todo_done:{todo_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def priority_label(value: str) -> str:
    return {"normal": "🟢 обычный", "high": "🟠 высокий", "urgent": "🔴 срочный"}.get(value, value)


def status_label(value: str) -> str:
    return {"new": "🆕 новая", "in_progress": "🛠 в работе", "done": "✅ выполнена"}.get(value, value)


def task_card(row: aiosqlite.Row, tz: ZoneInfo) -> str:
    description = f"\n<b>Описание:</b> {html.escape(row['description'])}" if row["description"] else ""
    order = html.escape(row["order_title"] or "Без заказа")
    return (
        f"🔧 <b>Задача №{row['id']}</b>\n"
        f"<b>Заказ:</b> {order}\n"
        f"<b>Что сделать:</b> {html.escape(row['title'])}{description}\n"
        f"<b>Ответственный:</b> {html.escape(row['assignee_name'])}\n"
        f"<b>Срок:</b> {local_dt_text(row['due_at_utc'], tz)}\n"
        f"<b>Приоритет:</b> {priority_label(row['priority'])}\n"
        f"<b>Статус:</b> {status_label(row['status'])}\n"
        f"<b>Поставил:</b> {html.escape(row['creator_name'])}"
    )


def todo_card(row: aiosqlite.Row, tz: ZoneInfo) -> str:
    return (
        f"📝 <b>Дело №{row['id']}</b>\n"
        f"<b>Что сделать:</b> {html.escape(row['title'])}\n"
        f"<b>Ответственный:</b> {html.escape(row['assignee_name'])}\n"
        f"<b>Срок:</b> {local_dt_text(row['due_at_utc'], tz)}\n"
        f"<b>Приоритет:</b> {priority_label(row['priority'])}\n"
        f"<b>Статус:</b> {status_label(row['status'])}\n"
        f"<b>Добавил:</b> {html.escape(row['creator_name'])}"
    )


def expense_card(row: aiosqlite.Row) -> str:
    receipt = "прикреплён" if row["receipt_file_id"] else "нет"
    return (
        f"💰 <b>Расход №{row['id']}</b>\n"
        f"<b>Сумма:</b> {money_text(row['amount'])}\n"
        f"<b>Категория:</b> {html.escape(row['category'])}\n"
        f"<b>Заказ:</b> {html.escape(row['order_title'] or 'Без заказа')}\n"
        f"<b>Описание:</b> {html.escape(row['description'] or '—')}\n"
        f"<b>Дата:</b> {row['expense_date']}\n"
        f"<b>Добавил:</b> {html.escape(row['creator_name'])}\n"
        f"<b>Чек:</b> {receipt}"
    )


def order_card(row: aiosqlite.Row) -> str:
    return (
        f"📦 <b>Заказ №{row['id']}</b>\n"
        f"<b>Объект:</b> {html.escape(row['title'])}\n"
        f"<b>Клиент:</b> {html.escape(row['client'] or '—')}\n"
        f"<b>Описание:</b> {html.escape(row['description'] or '—')}\n"
        f"<b>Создал:</b> {html.escape(row['creator_name'])}"
    )


def order_actions(row: aiosqlite.Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Добавить комментарий",
                    callback_data=f"ord_comment_add:{row['id']}",
                ),
                InlineKeyboardButton(
                    text=f"📜 Комментарии ({row['comment_count']})",
                    callback_data=f"ord_comments:{row['id']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📸 Добавить фото",
                    callback_data=f"ord_photo_add:{row['id']}",
                ),
                InlineKeyboardButton(
                    text=f"🖼 Фото ({row['photo_count']})",
                    callback_data=f"ord_photos:{row['id']}",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Состояния диалогов
# ---------------------------------------------------------------------------


class OrderForm(StatesGroup):
    title = State()
    client = State()
    description = State()


class TaskForm(StatesGroup):
    order = State()
    title = State()
    description = State()
    assignee = State()
    due = State()
    priority = State()


class TodoForm(StatesGroup):
    title = State()
    assignee = State()
    due = State()
    priority = State()


class ReminderForm(StatesGroup):
    text = State()
    destination = State()
    remind_at = State()


class ExpenseForm(StatesGroup):
    amount = State()
    category = State()
    order = State()
    description = State()
    receipt = State()


async def user_is_admin(telegram_id: int, db: Database, settings: Settings) -> bool:
    return telegram_id in settings.admin_ids or await db.is_admin(telegram_id)


class AccessMiddleware(BaseMiddleware):
    ADMIN_TEXTS = {
        "💵 Доходы",
        "📊 Отчёты",
        "👥 Сотрудники",
        "📅 Финансы сегодня",
        "📆 Финансы за месяц",
        "🗓 Выбрать период",
        "📦 Финансы по заказам",
        "📤 Выгрузить финансы",
        "📤 Выгрузить расходы",
        "📅 Расходы сегодня",
        "📆 Расходы за месяц",
        "⚠️ Сводка просрочек",
        "📦 Расходы по заказам",
        "🗑 Удалить расход",
        "➕ Добавить доход",
        "💵 Последние доходы",
        "➕ Добавить сотрудника",
        "📋 Активные сотрудники",
        "🚫 Заблокированные",
        "💵 Добавить доход",
        "📋 Доходы",
        "✅ Восстановить доступ",
        "🛡 Назначить администратора",
        "✏️ Изменить имя",
    }
    ADMIN_CALLBACK_PREFIXES = (
        "income_",
        "staff_",
        "expense_del",
        "comment_del",
        "pf_del",
        "v3_staff_",
        "v3_income_",
        "v3_agree_add",
    )
    ADMIN_COMMANDS = (
        "/reports",
        "/export_expenses",
        "/income",
        "/staff",
        "/adduser",
        "/blockuser",
    )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db: Database = data["db"]
        settings: Settings = data["settings"]
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        is_admin = await user_is_admin(user.id, db, settings)
        allowed = is_admin or await db.is_allowed(user.id)

        if isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/id"):
                return await handler(event, data)
            if not allowed:
                await event.answer(
                    "⛔ <b>Доступ запрещён.</b>\n"
                    f"Ваш Telegram ID: <code>{user.id}</code>\n"
                    "Передайте этот ID администратору бота."
                )
                return None
            if text in self.ADMIN_TEXTS or text.startswith(self.ADMIN_COMMANDS):
                if not is_admin:
                    await event.answer("⛔ Это действие доступно только администратору.")
                    return None
            if event.chat.type != ChatType.PRIVATE:
                if text.startswith("/chatid") and is_admin:
                    return await handler(event, data)
                if settings.work_chat_id is None or event.chat.id != settings.work_chat_id:
                    await event.answer("⛔ Этот групповой чат не разрешён для работы бота.")
                    return None

        if isinstance(event, CallbackQuery):
            if not allowed:
                await event.answer("Доступ к боту запрещён", show_alert=True)
                return None
            callback_data = event.data or ""
            if callback_data.startswith(self.ADMIN_CALLBACK_PREFIXES) and not is_admin:
                await event.answer("Это действие доступно только администратору", show_alert=True)
                return None
            message = event.message
            chat = getattr(message, "chat", None)
            if chat is not None and chat.type != ChatType.PRIVATE:
                if settings.work_chat_id is None or chat.id != settings.work_chat_id:
                    await event.answer("Этот чат не разрешён", show_alert=True)
                    return None

        return await handler(event, data)


router = Router()
router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


async def ensure_message_user(message: Message, db: Database) -> int:
    assert message.from_user is not None
    return await db.ensure_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )


async def ensure_callback_user(callback: CallbackQuery, db: Database) -> int:
    return await db.ensure_user(callback.from_user.id, callback.from_user.full_name, callback.from_user.username)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logging.warning("Не удалось отправить сообщение в chat_id=%s: %s", chat_id, exc)
        return False


async def can_manage(
    creator_telegram_id: int,
    assignee_telegram_id: int,
    actor_telegram_id: int,
    settings: Settings,
    db: Database,
) -> bool:
    return (
        actor_telegram_id in {creator_telegram_id, assignee_telegram_id}
        or await user_is_admin(actor_telegram_id, db, settings)
    )


# ---------------------------------------------------------------------------
# Общие команды
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    row = await db.user_by_telegram_id(message.from_user.id)
    is_admin = await user_is_admin(message.from_user.id, db, settings)
    if row is not None and not int(row["name_confirmed"]):
        await message.answer(
            "🏭 <b>Доступ разрешён.</b>\n\n"
            "Сначала укажите рабочее имя командой:\n"
            "<code>/register Алексей</code>",
            reply_markup=reply_keyboard([["✍️ Указать имя"]]),
        )
        return
    await message.answer(
        "🏭 <b>Рабочий бот «Фабрики Деталей»</b>\n\n"
        "Объекты, задачи, дела мастерской, финансы и портфолио.\n"
        f"Часовой пояс: <code>{html.escape(settings.timezone_name)}</code>",
        reply_markup=main_keyboard(is_admin),
    )


@router.message(Command("menu"))
@router.message(F.text == BTN_BACK)
async def show_main_menu(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    await message.answer(
        "Главное меню:",
        reply_markup=main_keyboard(await user_is_admin(message.from_user.id, db, settings)),
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel_form(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    await state.clear()
    assert message.from_user is not None
    await message.answer(
        "Действие отменено.",
        reply_markup=main_keyboard(await user_is_admin(message.from_user.id, db, settings)),
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    assert message.from_user is not None
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("chatid"))
async def cmd_chat_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("register"))
async def cmd_register(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    entered_name = (message.text or "").partition(" ")[2].strip()
    if entered_name:
        assert message.from_user is not None
        saved_name = entered_name[:100]
        await db.rename_user(message.from_user.id, saved_name)
        await message.answer(
            f"✅ Имя сотрудника сохранено: <b>{html.escape(saved_name)}</b>",
            reply_markup=main_keyboard(await user_is_admin(message.from_user.id, db, settings)),
        )
    else:
        await message.answer(
            "Чтобы сохранить или изменить имя, используйте:\n"
            "<code>/register Алексей</code>\n\n"
            "Либо нажмите кнопку «✍️ Указать имя»."
        )


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------


@router.message(F.text == "📦 Заказы")
async def orders_menu(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    await message.answer("Раздел заказов:", reply_markup=orders_keyboard())


@router.message(F.text == "➕ Новый заказ")
async def order_new(message: Message, state: FSMContext, db: Database) -> None:
    await ensure_message_user(message, db)
    await state.set_state(OrderForm.title)
    await message.answer("Введите название заказа или объекта:", reply_markup=reply_keyboard([["❌ Отмена"]]))


@router.message(OrderForm.title, F.text)
async def order_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Название слишком короткое. Введите ещё раз:")
        return
    await state.update_data(title=title[:200])
    await state.set_state(OrderForm.client)
    await message.answer("Введите имя клиента или нажмите «Пропустить»:", reply_markup=skip_keyboard())


@router.message(OrderForm.client, F.text)
async def order_client(message: Message, state: FSMContext) -> None:
    client = "" if message.text == "Пропустить" else (message.text or "").strip()
    await state.update_data(client=client[:200])
    await state.set_state(OrderForm.description)
    await message.answer("Добавьте комментарий к заказу или нажмите «Пропустить»:", reply_markup=skip_keyboard())


@router.message(OrderForm.description, F.text)
async def order_description(message: Message, state: FSMContext, db: Database) -> None:
    creator_id = await ensure_message_user(message, db)
    description = "" if message.text == "Пропустить" else (message.text or "").strip()
    data = await state.get_data()
    order_id = await db.create_order(data["title"], data["client"], description[:1000], creator_id)
    await state.clear()
    row = await db.order_by_id(order_id)
    assert row is not None
    await message.answer(order_card(row), reply_markup=order_actions(row))
    await message.answer("✅ Заказ создан.", reply_markup=orders_keyboard())


@router.message(F.text == "📋 Список заказов")
async def orders_list(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    rows = await db.active_orders()
    if not rows:
        await message.answer("Активных заказов пока нет.")
        return
    for row in rows:
        await message.answer(order_card(row), reply_markup=order_actions(row))


# ---------------------------------------------------------------------------
# Задачи
# ---------------------------------------------------------------------------


@router.message(F.text == "🔧 Задачи")
async def tasks_menu(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    await message.answer("Раздел задач:", reply_markup=tasks_keyboard())


@router.message(F.text == "➕ Новая задача")
async def task_new(message: Message, state: FSMContext, db: Database) -> None:
    await ensure_message_user(message, db)
    orders = await db.active_orders()
    await state.update_data(origin_chat_id=message.chat.id)
    await state.set_state(TaskForm.order)
    await message.answer("Выберите заказ:", reply_markup=orders_inline(orders, "task_order"))


@router.callback_query(TaskForm.order, F.data.startswith("task_order:"))
async def task_choose_order(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    await state.update_data(order_id=None if value == "none" else int(value))
    await state.set_state(TaskForm.title)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Что нужно сделать? Напишите название задачи:")


@router.message(TaskForm.title, F.text)
async def task_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Опишите задачу подробнее:")
        return
    await state.update_data(title=title[:300])
    await state.set_state(TaskForm.description)
    await message.answer("Добавьте подробности или нажмите «Пропустить»:", reply_markup=skip_keyboard())


@router.message(TaskForm.description, F.text)
async def task_description(message: Message, state: FSMContext, db: Database) -> None:
    description = "" if message.text == "Пропустить" else (message.text or "").strip()
    await state.update_data(description=description[:2000])
    users = await db.users()
    await state.set_state(TaskForm.assignee)
    await message.answer("Выберите ответственного:", reply_markup=staff_inline(users, "task_assignee"))


@router.callback_query(TaskForm.assignee, F.data.startswith("task_assignee:"))
async def task_choose_assignee(callback: CallbackQuery, state: FSMContext) -> None:
    assignee_id = int(callback.data.split(":", 1)[1])
    await state.update_data(assignee_id=assignee_id)
    await state.set_state(TaskForm.due)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Укажите срок. Примеры:\n"
            "<code>завтра 10:00</code>\n"
            "<code>18.07.2026 14:30</code>"
        )


@router.message(TaskForm.due, F.text)
async def task_due(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        due = parse_user_datetime(message.text or "", settings.timezone)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(due_at_utc=utc_iso(due))
    await state.set_state(TaskForm.priority)
    await message.answer("Выберите приоритет:", reply_markup=priority_inline("task_priority"))


@router.callback_query(TaskForm.priority, F.data.startswith("task_priority:"))
async def task_finish(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
    bot: Bot,
) -> None:
    creator_id = await ensure_callback_user(callback, db)
    priority = callback.data.split(":", 1)[1]
    data = await state.get_data()
    target_chat_id = settings.work_chat_id or int(data["origin_chat_id"])
    due = from_utc_iso(data["due_at_utc"])

    task_id = await db.create_task(
        chat_id=target_chat_id,
        order_id=data["order_id"],
        title=data["title"],
        description=data["description"],
        assignee_id=data["assignee_id"],
        creator_id=creator_id,
        priority=priority,
        due_at_utc=due,
    )
    row = await db.task_by_id(task_id)
    assert row is not None
    targets = [target_chat_id, int(row["assignee_telegram_id"])]
    await db.schedule_entity_reminders("task", task_id, creator_id, targets, row["title"], due)

    await callback.answer("Задача создана")
    await state.clear()
    sent = await safe_send(
        bot,
        target_chat_id,
        task_card(row, settings.timezone),
        reply_markup=task_actions(task_id, row["status"]),
    )
    if callback.message:
        note = "✅ Задача создана и опубликована." if sent else "✅ Задача создана, но не удалось отправить её в рабочий чат."
        await callback.message.answer(note, reply_markup=tasks_keyboard())


async def send_task_rows(message: Message, rows: Sequence[aiosqlite.Row], settings: Settings) -> None:
    if not rows:
        await message.answer("Подходящих задач нет.")
        return
    for row in rows:
        await message.answer(task_card(row, settings.timezone), reply_markup=task_actions(row["id"], row["status"]))


@router.message(F.text == "👤 Мои задачи")
async def my_tasks(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    await send_task_rows(message, await db.list_tasks("mine", message.from_user.id), settings)


@router.message(F.text == "📋 Все задачи")
async def all_tasks(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    await send_task_rows(message, await db.list_tasks("all"), settings)


@router.message(F.text == "⚠️ Просроченные задачи")
async def overdue_tasks_message(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    await send_task_rows(message, await db.list_tasks("overdue", now_utc=utc_now()), settings)


@router.callback_query(F.data.startswith("task_start:"))
async def task_start(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    row = await db.task_by_id(task_id)
    if not row:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    allowed = await can_manage(
        row["creator_telegram_id"], row["assignee_telegram_id"], callback.from_user.id, settings, db
    )
    if not allowed:
        await callback.answer("Менять задачу может ответственный, автор или администратор", show_alert=True)
        return
    await db.set_task_status(task_id, "in_progress")
    updated = await db.task_by_id(task_id)
    await callback.answer("Задача взята в работу")
    if callback.message and updated:
        await callback.message.edit_text(
            task_card(updated, settings.timezone), reply_markup=task_actions(task_id, updated["status"])
        )


@router.callback_query(F.data.startswith("task_done:"))
async def task_done(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    row = await db.task_by_id(task_id)
    if not row:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    allowed = await can_manage(
        row["creator_telegram_id"], row["assignee_telegram_id"], callback.from_user.id, settings, db
    )
    if not allowed:
        await callback.answer("Закрыть задачу может ответственный, автор или администратор", show_alert=True)
        return
    await db.set_task_status(task_id, "done")
    updated = await db.task_by_id(task_id)
    await callback.answer("Задача выполнена")
    if callback.message and updated:
        await callback.message.edit_text(task_card(updated, settings.timezone))


# ---------------------------------------------------------------------------
# Список дел
# ---------------------------------------------------------------------------


@router.message(F.text == "📝 Дела")
async def todos_menu(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    await message.answer("Список дел:", reply_markup=todos_keyboard())


@router.message(F.text == "➕ Новое дело (старое)")
async def todo_new(message: Message, state: FSMContext, db: Database) -> None:
    await ensure_message_user(message, db)
    await state.update_data(origin_chat_id=message.chat.id)
    await state.set_state(TodoForm.title)
    await message.answer("Что нужно сделать?", reply_markup=reply_keyboard([["❌ Отмена"]]))


@router.message(TodoForm.title, F.text)
async def todo_title(message: Message, state: FSMContext, db: Database) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Опишите дело подробнее:")
        return
    await state.update_data(title=title[:300])
    await state.set_state(TodoForm.assignee)
    await message.answer("Выберите ответственного:", reply_markup=staff_inline(await db.users(), "todo_assignee"))


@router.callback_query(TodoForm.assignee, F.data.startswith("todo_assignee:"))
async def todo_assignee(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(assignee_id=int(callback.data.split(":", 1)[1]))
    await state.set_state(TodoForm.due)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Укажите срок, например <code>завтра 17:00</code>:")


@router.message(TodoForm.due, F.text)
async def todo_due(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        due = parse_user_datetime(message.text or "", settings.timezone)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(due_at_utc=utc_iso(due))
    await state.set_state(TodoForm.priority)
    await message.answer("Выберите приоритет:", reply_markup=priority_inline("todo_priority"))


@router.callback_query(TodoForm.priority, F.data.startswith("todo_priority:"))
async def todo_finish(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
    bot: Bot,
) -> None:
    creator_id = await ensure_callback_user(callback, db)
    priority = callback.data.split(":", 1)[1]
    data = await state.get_data()
    target_chat_id = settings.work_chat_id or int(data["origin_chat_id"])
    due = from_utc_iso(data["due_at_utc"])
    todo_id = await db.create_todo(
        target_chat_id,
        data["title"],
        data["assignee_id"],
        creator_id,
        priority,
        due,
    )
    row = await db.todo_by_id(todo_id)
    assert row is not None
    await db.schedule_entity_reminders(
        "todo",
        todo_id,
        creator_id,
        [target_chat_id, int(row["assignee_telegram_id"])],
        row["title"],
        due,
    )
    await state.clear()
    await callback.answer("Дело создано")
    sent = await safe_send(
        bot,
        target_chat_id,
        todo_card(row, settings.timezone),
        reply_markup=todo_actions(todo_id, row["status"]),
    )
    if callback.message:
        note = "✅ Дело добавлено." if sent else "✅ Дело добавлено, но карточка не отправилась в рабочий чат."
        await callback.message.answer(note, reply_markup=todos_keyboard())


async def send_todo_rows(message: Message, rows: Sequence[aiosqlite.Row], settings: Settings) -> None:
    if not rows:
        await message.answer("Подходящих дел нет.")
        return
    for row in rows:
        await message.answer(todo_card(row, settings.timezone), reply_markup=todo_actions(row["id"], row["status"]))


@router.message(F.text == "👤 Мои дела")
async def my_todos(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    await send_todo_rows(message, await db.list_todos("mine", message.from_user.id), settings)


@router.message(F.text == "📋 Все дела")
async def all_todos(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    await send_todo_rows(message, await db.list_todos("all"), settings)


@router.message(F.text == "⚠️ Просроченные дела")
async def overdue_todos_message(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    await send_todo_rows(message, await db.list_todos("overdue", now_utc=utc_now()), settings)


@router.callback_query(F.data.startswith("todo_start:"))
async def todo_start(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    todo_id = int(callback.data.split(":", 1)[1])
    row = await db.todo_by_id(todo_id)
    if not row:
        await callback.answer("Дело не найдено", show_alert=True)
        return
    allowed = await can_manage(
        row["creator_telegram_id"], row["assignee_telegram_id"], callback.from_user.id, settings, db
    )
    if not allowed:
        await callback.answer("Менять дело может ответственный, автор или администратор", show_alert=True)
        return
    await db.set_todo_status(todo_id, "in_progress")
    updated = await db.todo_by_id(todo_id)
    await callback.answer("Дело взято в работу")
    if callback.message and updated:
        await callback.message.edit_text(
            todo_card(updated, settings.timezone), reply_markup=todo_actions(todo_id, updated["status"])
        )


@router.callback_query(F.data.startswith("todo_done:"))
async def todo_done(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    todo_id = int(callback.data.split(":", 1)[1])
    row = await db.todo_by_id(todo_id)
    if not row:
        await callback.answer("Дело не найдено", show_alert=True)
        return
    allowed = await can_manage(
        row["creator_telegram_id"], row["assignee_telegram_id"], callback.from_user.id, settings, db
    )
    if not allowed:
        await callback.answer("Закрыть дело может ответственный, автор или администратор", show_alert=True)
        return
    await db.set_todo_status(todo_id, "done")
    updated = await db.todo_by_id(todo_id)
    await callback.answer("Дело выполнено")
    if callback.message and updated:
        await callback.message.edit_text(todo_card(updated, settings.timezone))


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------


@router.message(Command("reminders"))
@router.message(F.text == "⏰ Напоминания")
async def reminders_menu(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    await message.answer("Напоминания:", reply_markup=reminders_keyboard())


@router.message(F.text == "➕ Новое напоминание")
async def reminder_new(message: Message, state: FSMContext, db: Database) -> None:
    await ensure_message_user(message, db)
    await state.update_data(origin_chat_id=message.chat.id)
    await state.set_state(ReminderForm.text)
    await message.answer("О чём напомнить?", reply_markup=reply_keyboard([["❌ Отмена"]]))


@router.message(ReminderForm.text, F.text)
async def reminder_text(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Введите текст напоминания:")
        return
    await state.update_data(text=text[:1000])
    await state.set_state(ReminderForm.destination)
    await message.answer(
        "Куда отправить напоминание?",
        reply_markup=reminder_destination_inline(settings.work_chat_id is not None),
    )


@router.callback_query(ReminderForm.destination, F.data.startswith("rem_dest:"))
async def reminder_destination(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    destination = callback.data.split(":", 1)[1]
    target_chat_id = callback.from_user.id if destination == "me" else settings.work_chat_id
    if target_chat_id is None:
        await callback.answer("Рабочий чат не настроен", show_alert=True)
        return
    await state.update_data(target_chat_id=target_chat_id)
    await state.set_state(ReminderForm.remind_at)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Когда напомнить? Например <code>сегодня 18:00</code> или <code>20.07.2026 09:00</code>:"
        )


@router.message(ReminderForm.remind_at, F.text)
async def reminder_finish(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        remind_at = parse_user_datetime(message.text or "", settings.timezone)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    creator_id = await ensure_message_user(message, db)
    data = await state.get_data()
    reminder_id = await db.add_reminder(
        "custom",
        None,
        creator_id,
        int(data["target_chat_id"]),
        data["text"],
        remind_at,
    )
    await state.clear()
    await message.answer(
        f"✅ Напоминание №{reminder_id} создано на {local_dt_text(utc_iso(remind_at), settings.timezone)}.",
        reply_markup=reminders_keyboard(),
    )


@router.message(F.text.in_({"📋 Мои напоминания", "📋 Активные напоминания"}))
async def reminder_list(message: Message, db: Database, settings: Settings) -> None:
    creator_id = await ensure_message_user(message, db)
    rows = await db.custom_reminders(creator_id)
    if not rows:
        await message.answer("Активных личных напоминаний нет.")
        return
    for row in rows:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"rem_cancel:{row['id']}")]
            ]
        )
        await message.answer(
            f"⏰ <b>Напоминание №{row['id']}</b>\n"
            f"{html.escape(row['text'])}\n"
            f"<b>Когда:</b> {local_dt_text(row['remind_at_utc'], settings.timezone)}",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("rem_cancel:"))
async def reminder_cancel(callback: CallbackQuery, db: Database) -> None:
    creator_id = await ensure_callback_user(callback, db)
    reminder_id = int(callback.data.split(":", 1)[1])
    if not await db.cancel_reminder(reminder_id, creator_id):
        await callback.answer("Напоминание не найдено или принадлежит другому пользователю", show_alert=True)
        return
    await callback.answer("Напоминание отменено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Расходы
# ---------------------------------------------------------------------------


EXPENSE_CATEGORIES = (
    "Металл",
    "Расходные материалы",
    "Краска",
    "Крепёж",
    "Доставка",
    "Инструмент",
    "Топливо",
    "Монтаж",
    "Прочее",
)


def categories_inline() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for index, category in enumerate(EXPENSE_CATEGORIES):
        pair.append(InlineKeyboardButton(text=category, callback_data=f"expense_cat:{index}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "💰 Расходы")
async def expenses_menu(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    await message.answer(
        "Учёт расходов:",
        reply_markup=expenses_keyboard(await user_is_admin(message.from_user.id, db, settings)),
    )


@router.message(F.text == "➕ Добавить расход (старое)")
async def expense_new(message: Message, state: FSMContext, db: Database) -> None:
    await ensure_message_user(message, db)
    await state.update_data(origin_chat_id=message.chat.id)
    await state.set_state(ExpenseForm.amount)
    await message.answer("Введите сумму расхода, например <code>4850</code>:", reply_markup=reply_keyboard([["❌ Отмена"]]))


@router.message(ExpenseForm.amount, F.text)
async def expense_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(amount=str(amount))
    await state.set_state(ExpenseForm.category)
    await message.answer("Выберите категорию:", reply_markup=categories_inline())


@router.callback_query(ExpenseForm.category, F.data.startswith("expense_cat:"))
async def expense_category(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    index = int(callback.data.split(":", 1)[1])
    if not 0 <= index < len(EXPENSE_CATEGORIES):
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.update_data(category=EXPENSE_CATEGORIES[index])
    await state.set_state(ExpenseForm.order)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "К какому заказу относится расход?",
            reply_markup=orders_inline(await db.active_orders(), "expense_order"),
        )


@router.callback_query(ExpenseForm.order, F.data.startswith("expense_order:"))
async def expense_order(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    await state.update_data(order_id=None if value == "none" else int(value))
    await state.set_state(ExpenseForm.description)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Что было куплено или оплачено?", reply_markup=reply_keyboard([["❌ Отмена"]]))


@router.message(ExpenseForm.description, F.text)
async def expense_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if len(description) < 2:
        await message.answer("Добавьте короткое описание расхода:")
        return
    await state.update_data(description=description[:1000])
    await state.set_state(ExpenseForm.receipt)
    await message.answer("Отправьте фотографию чека или нажмите «Пропустить»:", reply_markup=skip_keyboard())


async def save_expense(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    bot: Bot,
    receipt_file_id: str | None,
) -> None:
    creator_id = await ensure_message_user(message, db)
    data = await state.get_data()
    expense_day = datetime.now(settings.timezone).date()
    expense_id = await db.create_expense(
        data["order_id"],
        Decimal(data["amount"]),
        data["category"],
        data["description"],
        receipt_file_id,
        creator_id,
        expense_day,
    )
    row = await db.expense_by_id(expense_id)
    assert row is not None
    await state.clear()
    target_chat_id = settings.work_chat_id or int(data["origin_chat_id"])
    try:
        if receipt_file_id:
            await bot.send_photo(target_chat_id, receipt_file_id, caption=expense_card(row))
        else:
            await bot.send_message(target_chat_id, expense_card(row))
        sent = True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logging.warning("Не удалось отправить расход в chat_id=%s: %s", target_chat_id, exc)
        sent = False
    note = "✅ Расход записан." if sent else "✅ Расход записан, но карточка не отправилась в рабочий чат."
    assert message.from_user is not None
    await message.answer(
        note,
        reply_markup=expenses_keyboard(await user_is_admin(message.from_user.id, db, settings)),
    )


@router.message(ExpenseForm.receipt, F.photo)
async def expense_receipt_photo(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    bot: Bot,
) -> None:
    assert message.photo
    await save_expense(message, state, db, settings, bot, message.photo[-1].file_id)


@router.message(ExpenseForm.receipt, F.text == "Пропустить")
async def expense_receipt_skip(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    bot: Bot,
) -> None:
    await save_expense(message, state, db, settings, bot, None)


@router.message(ExpenseForm.receipt)
async def expense_receipt_invalid(message: Message) -> None:
    await message.answer("Отправьте фотографию чека или нажмите «Пропустить».")


@router.message(F.text == "🧾 Последние расходы")
async def expenses_recent(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    assert message.from_user is not None
    admin = await user_is_admin(message.from_user.id, db, settings)
    if admin:
        rows = await db.recent_expenses()
    else:
        rows = await db.fetchall(
            """
            SELECT e.*, u.full_name AS creator_name, o.title AS order_title
            FROM expenses e
            JOIN users u ON u.id=e.created_by
            LEFT JOIN orders o ON o.id=e.order_id
            WHERE e.deleted=0 AND u.telegram_id=?
            ORDER BY e.id DESC LIMIT 10
            """,
            (message.from_user.id,),
        )
    if not rows:
        await message.answer("Расходов пока нет.")
        return
    for row in rows:
        markup = None
        if admin:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🗑 Удалить расход",
                        callback_data=f"expense_del_ask:{row['id']}",
                    )
                ]]
            )
        if row["receipt_file_id"]:
            try:
                await message.answer_photo(
                    row["receipt_file_id"],
                    caption=expense_card(row),
                    reply_markup=markup,
                )
                continue
            except TelegramBadRequest:
                pass
        await message.answer(expense_card(row), reply_markup=markup)


async def export_month_expenses(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    today = datetime.now(settings.timezone).date()
    start = today.replace(day=1)
    rows = await db.expenses_for_period(start, today)
    if not rows:
        await message.answer("В этом месяце расходов пока нет.")
        return

    text_buffer = io.StringIO()
    writer = csv.writer(text_buffer, delimiter=";")
    writer.writerow(["ID", "Дата", "Сумма", "Категория", "Заказ", "Описание", "Добавил", "Чек"])
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["expense_date"],
                row["amount"],
                row["category"],
                row["order_title"],
                row["description"],
                row["creator_name"],
                row["has_receipt"],
            ]
        )
    data = ("\ufeff" + text_buffer.getvalue()).encode("utf-8")
    filename = f"expenses_{start.isoformat()}_{today.isoformat()}.csv"
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption="📤 Выгрузка расходов за текущий месяц. Файл открывается в Excel.",
    )


@router.message(Command("export_expenses"))
@router.message(F.text == "📤 Выгрузить расходы")
async def expenses_export(message: Message, db: Database, settings: Settings) -> None:
    await export_month_expenses(message, db, settings)


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------


@router.message(Command("reports_legacy_disabled"))
@router.message(F.text == "📊 Отчёты (старое)")
async def reports_menu(message: Message, db: Database) -> None:
    await ensure_message_user(message, db)
    await message.answer("Выберите отчёт:", reply_markup=reports_keyboard())


async def send_expense_summary(message: Message, db: Database, start: date, end: date, title: str) -> None:
    total, rows = await db.expense_summary(start, end)
    lines = [f"📊 <b>{html.escape(title)}</b>"]
    if not rows:
        lines.append("\nРасходов нет.")
    else:
        for row in rows:
            lines.append(f"\n{html.escape(row['category'])} — {money_text(row['total'])}")
        lines.append(f"\n\n<b>Итого: {money_text(total)}</b>")
    await message.answer("".join(lines))


@router.message(F.text == "📅 Расходы сегодня")
async def report_today(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    today = datetime.now(settings.timezone).date()
    await send_expense_summary(message, db, today, today, f"Расходы за {today.strftime('%d.%m.%Y')}")


@router.message(F.text == "📆 Расходы за месяц")
async def report_month(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    today = datetime.now(settings.timezone).date()
    await send_expense_summary(message, db, today.replace(day=1), today, "Расходы за текущий месяц")


@router.message(F.text == "📦 Расходы по заказам")
async def report_orders(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    today = datetime.now(settings.timezone).date()
    rows = await db.order_expense_summary(today.replace(day=1), today)
    if not rows:
        await message.answer("В этом месяце расходов нет.")
        return
    lines = ["📦 <b>Расходы по заказам за месяц</b>"]
    for row in rows:
        lines.append(f"\n{html.escape(row['order_title'])} — {money_text(row['total'])}")
    await message.answer("".join(lines))


@router.message(F.text == "⚠️ Сводка просрочек")
async def report_overdue(message: Message, db: Database, settings: Settings) -> None:
    await ensure_message_user(message, db)
    tasks = await db.list_tasks("overdue", now_utc=utc_now(), limit=50)
    todos = await db.list_todos("overdue", now_utc=utc_now(), limit=50)
    lines = ["⚠️ <b>Сводка просрочек</b>"]
    if not tasks and not todos:
        lines.append("\nПросрочек нет.")
    if tasks:
        lines.append("\n\n<b>Задачи:</b>")
        for row in tasks:
            lines.append(
                f"\n№{row['id']} {html.escape(row['title'])} — {html.escape(row['assignee_name'])} "
                f"({local_dt_text(row['due_at_utc'], settings.timezone)})"
            )
    if todos:
        lines.append("\n\n<b>Дела:</b>")
        for row in todos:
            lines.append(
                f"\n№{row['id']} {html.escape(row['title'])} — {html.escape(row['assignee_name'])} "
                f"({local_dt_text(row['due_at_utc'], settings.timezone)})"
            )
    await message.answer("".join(lines))


# ---------------------------------------------------------------------------
# Помощь
# ---------------------------------------------------------------------------


@router.message(F.text == "⚙️ Помощь")
async def help_message(message: Message, db: Database, settings: Settings) -> None:
    assert message.from_user is not None
    admin = await user_is_admin(message.from_user.id, db, settings)
    text = (
        "⚙️ <b>Команды бота</b>\n\n"
        "<code>/start</code> — открыть меню\n"
        "<code>/register Имя</code> — сохранить рабочее имя\n"
        "<code>/id</code> — узнать свой Telegram ID\n"
        "<code>/chatid</code> — узнать ID текущего чата\n"
        "<code>/portfolio</code> — портфолио объектов\n"
        "<code>/cancel</code> — отменить текущий ввод\n"
    )
    if admin:
        text += (
            "<code>/income</code> — доходы\n"
            "<code>/staff</code> — управление доступом\n"
            "<code>/adduser ID</code> — дать сотруднику доступ\n"
            "<code>/blockuser ID</code> — заблокировать доступ\n"
        )
    text += (
        "\nЧтобы получать личные напоминания, сотрудник должен открыть бота, "
        "нажать /start и указать рабочее имя."
    )
    await message.answer(text)


# ---------------------------------------------------------------------------
# Фоновая отправка напоминаний
# ---------------------------------------------------------------------------


async def reminder_worker(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        try:
            now = utc_now()
            due = await db.due_reminders(now)
            for row in due:
                sent = await safe_send(
                    bot,
                    int(row["target_chat_id"]),
                    f"⏰ <b>Напоминание</b>\n{html.escape(row['text'])}",
                )
                if sent:
                    await db.mark_reminder_sent(int(row["id"]))
                else:
                    await db.cancel_reminder(int(row["id"]))

            local_day = now.astimezone(settings.timezone).date()
            for row in await db.overdue_tasks(now, local_day):
                text = (
                    f"⚠️ <b>Просрочена задача №{row['id']}</b>\n"
                    f"{html.escape(row['title'])}\n"
                    f"Ответственный: {html.escape(row['assignee_name'])}\n"
                    f"Срок был: {local_dt_text(row['due_at_utc'], settings.timezone)}"
                )
                targets: set[int] = set()
                source_chat = int(row["chat_id"])
                if source_chat < 0:
                    if settings.work_chat_id == source_chat:
                        targets.add(source_chat)
                elif await db.is_allowed(source_chat):
                    targets.add(source_chat)
                assignee_chat = int(row["assignee_telegram_id"])
                if await db.is_allowed(assignee_chat):
                    targets.add(assignee_chat)
                for target in targets:
                    await safe_send(bot, target, text)
                await db.mark_overdue_notified("task", int(row["id"]), local_day)

            for row in await db.overdue_todos(now, local_day):
                text = (
                    f"⚠️ <b>Просрочено дело №{row['id']}</b>\n"
                    f"{html.escape(row['title'])}\n"
                    f"Ответственный: {html.escape(row['assignee_name'])}\n"
                    f"Срок был: {local_dt_text(row['due_at_utc'], settings.timezone)}"
                )
                targets: set[int] = set()
                source_chat = int(row["chat_id"])
                if source_chat < 0:
                    if settings.work_chat_id == source_chat:
                        targets.add(source_chat)
                elif await db.is_allowed(source_chat):
                    targets.add(source_chat)
                assignee_chat = int(row["assignee_telegram_id"])
                if await db.is_allowed(assignee_chat):
                    targets.add(assignee_chat)
                for target in targets:
                    await safe_send(bot, target, text)
                await db.mark_overdue_notified("todo", int(row["id"]), local_day)

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка фоновой проверки напоминаний")

        await asyncio.sleep(30)


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="register", description="Указать рабочее имя"),
        BotCommand(command="tasks", description="Мои задачи"),
        BotCommand(command="todos", description="Дела мастерской"),
        BotCommand(command="expenses", description="Финансы"),
        BotCommand(command="portfolio", description="Портфолио"),
        BotCommand(command="reminders", description="Напоминания"),
        BotCommand(command="income", description="Доходы для администратора"),
        BotCommand(command="staff", description="Сотрудники для администратора"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
        BotCommand(command="id", description="Мой Telegram ID"),
        BotCommand(command="chatid", description="ID текущего чата"),
    ]
    await bot.set_my_commands(commands)


from extensions import register_extension_handlers  # noqa: E402

register_extension_handlers(router)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings.from_env()
    settings.portfolio_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.init()
    await db.seed_access(settings.admin_ids, settings.allowed_ids)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await set_bot_commands(bot)
    worker = asyncio.create_task(reminder_worker(bot, db, settings), name="reminder-worker")
    try:
        logging.info("Бот запущен. Часовой пояс: %s", settings.timezone_name)
        await dp.start_polling(
            bot,
            db=db,
            settings=settings,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
