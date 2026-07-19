from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from bot import Database, utc_iso, utc_now


async def check() -> None:
    with tempfile.TemporaryDirectory(prefix="factory_bot_v3_1_") as folder:
        db = Database(Path(folder) / "check.sqlite3")
        await db.init()
        required_tables = {
            "users",
            "orders",
            "tasks",
            "todos",
            "expenses",
            "expense_receipts",
            "object_recent_views",
            "reminders",
            "portfolio_photos",
            "incomes",
            "agreements",
            "object_status_history",
            "task_comments",
            "task_photos",
            "todo_comments",
            "todo_photos",
        }
        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        actual = {row["name"] for row in rows}
        missing = sorted(required_tables - actual)
        if missing:
            raise RuntimeError(f"Не созданы таблицы: {', '.join(missing)}")

        now = utc_iso(utc_now())
        user_id = await db.execute(
            """INSERT INTO users(telegram_id,full_name,username,active,created_at_utc,role,name_confirmed)
               VALUES(123456,'Проверка',NULL,1,?,'admin',1)""",
            (now,),
        )
        object_id = await db.execute(
            """INSERT INTO orders(
                   title,client,description,status,created_by,created_at_utc,category,address,
                   client_phone,client_telegram,responsible_id,due_at_utc,object_status,updated_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Тестовый навес",
                "Заказчик",
                None,
                "active",
                user_id,
                now,
                "🏠 Навес",
                None,
                None,
                None,
                user_id,
                None,
                "new",
                now,
            ),
        )
        await db.execute(
            "INSERT INTO object_recent_views(user_id,order_id,viewed_at_utc) VALUES(?,?,?)",
            (user_id, object_id, now),
        )
        expense_id = await db.execute(
            """INSERT INTO expenses(
                   order_id,amount,category,description,receipt_file_id,created_by,
                   expense_date,created_at_utc,deleted
               ) VALUES(?,?,?,?,?,?,?,?,0)""",
            (object_id, "1000.00", "🧱 Материалы", "Проверка", "legacy-file", user_id, "2026-07-18", now),
        )

        # Повторный init должен безопасно выполнить миграции и перенести старый одиночный чек.
        await db.init()
        receipt = await db.fetchone(
            "SELECT telegram_file_id FROM expense_receipts WHERE expense_id=?",
            (expense_id,),
        )
        if not receipt or receipt["telegram_file_id"] != "legacy-file":
            raise RuntimeError("Не выполнена миграция старого фото чека")


if __name__ == "__main__":
    asyncio.run(check())
    print("OK: версия 3.1.0 готова к запуску")
