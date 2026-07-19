from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)


# ---------------------------------------------------------------------------
# Общие функции и клавиатуры версии 3
# ---------------------------------------------------------------------------


RECEIPT_LOCKS: dict[int, asyncio.Lock] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_value(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def dt_from_iso(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def local_dt(value: str | None, tz: Any) -> str:
    if not value:
        return "без срока"
    return dt_from_iso(value).astimezone(tz).strftime("%d.%m.%Y %H:%M")


def local_day(value: str, tz: Any) -> date:
    return dt_from_iso(value).astimezone(tz).date()


def money(value: Any) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} ₽"


def parse_amount(text: str) -> Decimal:
    try:
        amount = Decimal(text.strip().replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Введите сумму числом, например 15000") from exc
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return amount.quantize(Decimal("0.01"))


def kb(rows: list[list[str]], placeholder: str = "Выберите действие") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=value) for value in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def objects_menu() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["➕ Новый объект"],
            ["🕓 Недавние объекты", "📋 Все активные"],
            ["✅ Завершённые объекты", "🔍 Найти объект"],
            ["⬅️ Главное меню"],
        ]
    )


def my_tasks_menu() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["🔥 На сегодня", "📅 Ближайшие"],
            ["⚠️ Просроченные", "▶️ В работе"],
            ["✅ Выполненные задачи"],
            ["⬅️ Главное меню"],
        ]
    )


def workshop_menu() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["➕ Новое дело"],
            ["📋 Активные дела", "👤 Мои дела"],
            ["⚠️ Просроченные дела", "✅ Выполненные дела"],
            ["🔁 Регулярные дела"],
            ["⬅️ Главное меню"],
        ]
    )


def finance_menu(admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        ["➕ Добавить расход"],
        ["📋 Расходы", "📦 Финансы по объектам"],
        ["🏭 Расходы мастерской"],
    ]
    if admin:
        rows.insert(1, ["💵 Добавить доход", "📋 Доходы"])
        rows.append(["📤 Выгрузить финансы"])
    rows.append(["⬅️ Главное меню"])
    return kb(rows)


def portfolio_menu() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["📦 Фото по объектам", "🏷 Фото по категориям"],
            ["🕓 Недавно добавленные", "⭐ Лучшие работы"],
            ["↔️ До и после", "🌐 Для сайта"],
            ["⬅️ Главное меню"],
        ]
    )


def staff_menu() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["➕ Добавить сотрудника"],
            ["📋 Активные сотрудники", "🚫 Заблокированные сотрудники"],
            ["✅ Восстановить доступ"],
            ["🛡 Назначить администратора"],
            ["✏️ Изменить имя"],
            ["⬅️ Главное меню"],
        ]
    )


def date_keyboard(allow_none: bool = True) -> ReplyKeyboardMarkup:
    rows = [
        ["Сегодня", "Завтра"],
        ["Послезавтра", "Через неделю"],
        ["🗓 Ввести дату"],
    ]
    if allow_none:
        rows.append(["Без срока"])
    rows.append(["❌ Отмена"])
    return kb(rows, "Выберите дату или введите её сообщением")


def time_keyboard() -> ReplyKeyboardMarkup:
    return kb(
        [
            ["09:00", "12:00"],
            ["15:00", "18:00"],
            ["До конца дня", "⌨️ Другое время"],
            ["❌ Отмена"],
        ],
        "Выберите или введите время",
    )


def parse_date_choice(text: str, tz: Any, *, allow_past: bool = False) -> date | None:
    raw = " ".join(text.strip().lower().split())
    today = datetime.now(tz).date()
    if raw == "без срока":
        return None
    shortcuts = {
        "сегодня": today,
        "завтра": today + timedelta(days=1),
        "послезавтра": today + timedelta(days=2),
        "через неделю": today + timedelta(days=7),
    }
    if raw in shortcuts:
        return shortcuts[raw]
    if raw in {"🗓 ввести дату", "ввести дату"}:
        raise ValueError("Введите дату, например <code>25.07.2026</code>")
    result: date | None = None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            if fmt == "%d.%m":
                parsed = parsed.replace(year=today.year)
                if not allow_past and parsed < today:
                    parsed = parsed.replace(year=today.year + 1)
            result = parsed
            break
        except ValueError:
            continue
    if result is None:
        raise ValueError("Не понял дату. Пример: <code>25.07.2026</code>")
    if not allow_past and result < today:
        raise ValueError("Дата не может быть в прошлом")
    return result


def parse_time_choice(text: str) -> time:
    raw = text.strip().lower()
    if raw == "до конца дня":
        return time(18, 0)
    if raw in {"⌨️ другое время", "другое время"}:
        raise ValueError("Введите время, например <code>16:30</code>")
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Не понял время. Пример: <code>16:30</code>") from exc


def combine_due(day: date, clock: time, tz: Any) -> datetime:
    value = datetime.combine(day, clock, tzinfo=tz)
    if value <= datetime.now(tz):
        raise ValueError("Дата и время должны быть в будущем")
    return value.astimezone(timezone.utc)


async def is_admin(user_id: int, db: Any, settings: Any) -> bool:
    return user_id in settings.admin_ids or await db.is_admin(user_id)


async def actor_id(event: Message | CallbackQuery, db: Any) -> int:
    user = event.from_user
    return await db.ensure_user(user.id, user.full_name, user.username)


async def safe_notify(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def fetch_object(db: Any, object_id: int) -> aiosqlite.Row | None:
    return await db.fetchone(
        """
        SELECT o.*, c.full_name creator_name,
               r.full_name responsible_name, r.telegram_id responsible_telegram_id,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status<>'done') open_tasks,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id) total_tasks,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status='done') done_tasks,
               (SELECT COUNT(*) FROM order_comments x WHERE x.order_id=o.id AND x.deleted=0) comment_count,
               (SELECT COUNT(*) FROM portfolio_photos p WHERE p.order_id=o.id AND p.deleted=0) photo_count,
               (SELECT COUNT(*) FROM agreements a WHERE a.order_id=o.id AND a.deleted=0) agreement_count
        FROM orders o
        JOIN users c ON c.id=o.created_by
        LEFT JOIN users r ON r.id=o.responsible_id
        WHERE o.id=?
        """,
        (object_id,),
    )


async def list_objects(db: Any, mode: str = "active", query: str | None = None) -> list[aiosqlite.Row]:
    where: list[str] = []
    params: list[Any] = []
    if mode == "active":
        where.append("o.status='active'")
    elif mode == "completed":
        where.append("o.status IN ('completed','cancelled')")
    if query:
        where.append(
            "(LOWER(o.title) LIKE ? OR LOWER(COALESCE(o.client,'')) LIKE ? "
            "OR LOWER(COALESCE(o.address,'')) LIKE ? OR COALESCE(o.client_phone,'') LIKE ?)"
        )
        pattern = f"%{query.lower()}%"
        params.extend([pattern, pattern, pattern, f"%{query}%"])
    sql_where = " AND ".join(where) if where else "1=1"
    params.append(60)
    return await db.fetchall(
        f"""
        SELECT o.*, c.full_name creator_name, r.full_name responsible_name,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status<>'done') open_tasks,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id) total_tasks,
               (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status='done') done_tasks,
               (SELECT COUNT(*) FROM portfolio_photos p WHERE p.order_id=o.id AND p.deleted=0) photo_count
        FROM orders o
        JOIN users c ON c.id=o.created_by
        LEFT JOIN users r ON r.id=o.responsible_id
        WHERE {sql_where}
        ORDER BY CASE o.object_status
            WHEN 'measurement' THEN 1 WHEN 'design' THEN 2 WHEN 'manufacturing' THEN 3
            WHEN 'painting' THEN 4 WHEN 'installation' THEN 5 ELSE 6 END, o.id DESC
        LIMIT ?
        """,
        params,
    )


CATEGORY_OPTIONS = {
    "fence": "🚧 Забор",
    "canopy": "🏠 Навес",
    "gates": "🚪 Ворота",
    "other": "📌 Другое",
}

OBJECT_STATUSES = {
    "new": "🆕 Новый",
    "measurement": "📐 Замер",
    "design": "📏 Проектирование",
    "manufacturing": "🔩 Изготовление",
    "painting": "🎨 Покраска",
    "installation": "🛠 Монтаж",
    "completed": "✅ Завершён",
    "paused": "⏸ Приостановлен",
    "cancelled": "❌ Отменён",
}

PHOTO_STAGES = {
    "measurement": "📐 Замер",
    "manufacturing": "🔩 Изготовление",
    "installation": "🛠 Монтаж",
    "finished": "✅ Готовый объект",
}

WORKSHOP_CATEGORIES = {
    "equipment": "🔧 Оборудование",
    "premises": "🏭 Цех и помещение",
    "transport": "🚗 Транспорт",
    "payments": "💳 Платежи",
    "household": "🧹 Хозяйственные дела",
    "other": "📌 Прочее",
}

EXPENSE_CATEGORIES = (
    "🧱 Материалы",
    "🛠 Инструмент",
    "🏢 Аренда",
    "🧹 Хозтовары",
    "📣 Реклама",
    "📌 Прочие расходы",
)

INCOME_TYPES = ("💳 Аванс", "➕ Доплата", "✅ Окончательный расчёт", "📌 Другой платёж")

TASK_TEMPLATES = {
    "🚧 Забор": [
        "Выполнить замер",
        "Рассчитать материалы",
        "Закупить материалы",
        "Изготовить секции",
        "Подготовить место установки",
        "Выполнить монтаж",
        "Сделать итоговые фотографии",
    ],
    "🏠 Навес": [
        "Выполнить замер",
        "Подготовить чертёж",
        "Закупить материалы",
        "Изготовить каркас",
        "Выполнить покраску",
        "Выполнить монтаж",
        "Сделать итоговые фотографии",
    ],
    "🚪 Ворота": [
        "Выполнить замер",
        "Подготовить чертёж",
        "Закупить материалы и фурнитуру",
        "Изготовить каркас",
        "Установить механизмы",
        "Выполнить покраску",
        "Выполнить монтаж",
        "Проверить работу и сделать фотографии",
    ],
}


def object_category_markup() -> InlineKeyboardMarkup:
    return inline(
        [
            [("🚧 Забор", "v3_oc:fence"), ("🏠 Навес", "v3_oc:canopy")],
            [("🚪 Ворота", "v3_oc:gates"), ("📌 Другое", "v3_oc:other")],
            [("❌ Отмена", "v3_cancel")],
        ]
    )


def object_create_markup(data: dict[str, Any]) -> InlineKeyboardMarkup:
    template_label = "📋 Шаблон: включён" if data.get("use_template", True) else "📋 Шаблон: без задач"
    return inline(
        [
            [("✅ Создать объект", "v3_ocreate")],
            [("👤 Ответственный", "v3_oquick_responsible"), ("📅 Срок", "v3_oquick_due")],
            [(template_label, "v3_oquick_template")],
            [("➕ Добавить подробности", "v3_odetails")],
            [("❌ Отмена", "v3_cancel")],
        ]
    )


def object_details_markup(data: dict[str, Any]) -> InlineKeyboardMarkup:
    def mark(value: Any, title: str) -> str:
        return ("✅ " if value else "➕ ") + title

    contacts_filled = any(data.get(key) for key in ("client_name", "client_phone", "client_telegram"))
    return inline(
        [
            [(mark(data.get("address"), "Адрес"), "v3_odetail:address"),
             (mark(data.get("description"), "Описание"), "v3_odetail:description")],
            [(mark(contacts_filled, "Контакты заказчика"), "v3_odetail:contacts")],
            [(mark(data.get("initial_agreement"), "Договорённость"), "v3_odetail:agreement")],
            [(mark(data.get("responsible_id"), "Ответственный"), "v3_odetail:responsible"),
             (mark(data.get("due_at_utc"), "Срок"), "v3_odetail:due")],
            [("📋 Настроить шаблон", "v3_odetail:template"),
             ("📊 Статус объекта", "v3_odetail:status")],
            [("⬅️ Назад к созданию", "v3_opreview")],
            [("✅ Создать объект", "v3_ocreate")],
            [("❌ Отмена", "v3_cancel")],
        ]
    )


def contact_details_markup(data: dict[str, Any]) -> InlineKeyboardMarkup:
    def mark(value: Any, title: str) -> str:
        return ("✅ " if value else "➕ ") + title
    return inline(
        [
            [(mark(data.get("client_name"), "Имя"), "v3_ocontact:name")],
            [(mark(data.get("client_phone"), "Телефон"), "v3_ocontact:phone")],
            [(mark(data.get("client_telegram"), "Telegram"), "v3_ocontact:telegram")],
            [("⬅️ К подробностям", "v3_odetails")],
        ]
    )


def compact_objects_text(rows: Sequence[aiosqlite.Row], tz: Any, title: str) -> str:
    lines = [f"<b>{html.escape(title)}</b>"]
    now_local = datetime.now(tz)
    for index, row in enumerate(rows, start=1):
        status = OBJECT_STATUSES.get(str(row["object_status"]), str(row["object_status"]))
        due_value = row["due_at_utc"]
        if due_value:
            due_dt = dt_from_iso(due_value).astimezone(tz)
            if due_dt < now_local and row["status"] == "active":
                due_text = f"⚠️ просрочен с {due_dt.strftime('%d.%m')}"
            else:
                due_text = f"до {due_dt.strftime('%d.%m')}"
        else:
            due_text = "без срока"
        tasks = f"{row['done_tasks']} из {row['total_tasks']}"
        lines.extend(
            [
                "",
                f"<b>{index}. {html.escape(row['category'] or '📌 Другое')} — {html.escape(row['title'])}</b>",
                f"{status} · {due_text}",
                f"✅ Задачи: {tasks}",
            ]
        )
    return "\n".join(lines)


def compact_objects_markup(rows: Sequence[aiosqlite.Row]) -> InlineKeyboardMarkup:
    return inline(
        [[(f"{index}. {str(row['title'])[:42]}", f"v3_openobj:{row['id']}")]]
        for index, row in enumerate(rows, start=1)
    )


def users_markup(rows: Sequence[aiosqlite.Row], prefix: str, allow_none: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [(str(row["full_name"])[:48], f"{prefix}:{row['id']}")]
        for row in rows[:45]
    ]
    if allow_none:
        buttons.append([("Пока не назначать", f"{prefix}:none")])
    return inline(buttons or [[("Нет доступных сотрудников", "noop")]])


def objects_markup(rows: Sequence[aiosqlite.Row], prefix: str, allow_none: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [(f"№{row['id']} — {str(row['title'])[:38]}", f"{prefix}:{row['id']}")]
        for row in rows[:45]
    ]
    if allow_none:
        buttons.append([("Без привязки", f"{prefix}:none")])
    return inline(buttons or [[("Объектов нет", "noop")]])


def stages_markup(prefix: str, include_all: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if include_all:
        rows.append([("🖼 Все этапы", f"{prefix}:all")])
    rows.extend([[(label, f"{prefix}:{key}")] for key, label in PHOTO_STAGES.items()])
    return inline(rows)


def object_status_markup(object_id: int) -> InlineKeyboardMarkup:
    return inline(
        [[(label, f"v3_oset:{object_id}:{key}")] for key, label in OBJECT_STATUSES.items()]
    )


def object_card(row: aiosqlite.Row, tz: Any) -> str:
    status = OBJECT_STATUSES.get(str(row["object_status"]), str(row["object_status"]))
    tasks = f"{row['done_tasks']} из {row['total_tasks']}" if "done_tasks" in row.keys() else "—"
    lines = [
        f"📦 <b>Объект №{row['id']}</b>",
        f"🏷 <b>Категория:</b> {html.escape(row['category'] or 'Другое')}",
        f"✏️ <b>Название:</b> {html.escape(row['title'])}",
        f"📊 <b>Статус:</b> {status}",
        f"👤 <b>Ответственный:</b> {html.escape(row['responsible_name'] or 'не назначен')}",
        f"📅 <b>Срок:</b> {local_dt(row['due_at_utc'], tz)}",
        f"✅ <b>Задачи:</b> {tasks}",
    ]
    if row["address"]:
        lines.append(f"📍 <b>Адрес:</b> {html.escape(row['address'])}")
    if row["description"]:
        lines.append(f"📝 <b>Описание:</b> {html.escape(row['description'])}")
    return "\n".join(lines)


def object_actions(row: aiosqlite.Row, admin: bool) -> InlineKeyboardMarkup:
    oid = int(row["id"])
    rows: list[list[tuple[str, str]]] = [
        [("➕ Добавить задачу", f"v3_tadd:{oid}"), ("📋 Задачи объекта", f"v3_otasks:{oid}")],
        [("📊 Изменить статус", f"v3_ostatus:{oid}"), ("👤 Контакты", f"v3_contacts:{oid}")],
        [("🤝 Договорённости", f"v3_agreements:{oid}"), ("💬 Комментарии", f"v3_ocomments:{oid}")],
        [("📸 Добавить фото", f"v3_padd:{oid}"), ("🖼 Фотографии", f"v3_pview:{oid}")],
        [("↔️ До и после", f"v3_beforeafter:{oid}"), ("💰 Финансы", f"v3_ofin:{oid}")],
        [("💰 Добавить расход", f"v3_exp_o:{oid}")],
    ]
    if admin:
        rows[-1].append(("💵 Добавить доход", f"v3_inc_o:{oid}"))
        rows.append([("✏️ Изменить данные", f"v3_oedit:{oid}")])
    return inline(rows)


def task_card(row: aiosqlite.Row, tz: Any) -> str:
    statuses = {"new": "🆕 Новая", "in_progress": "▶️ В работе", "done": "✅ Выполнена"}
    priorities = {"normal": "🟢 Обычный", "high": "🟠 Высокий", "urgent": "🔴 Срочный"}
    lines = [
        f"✅ <b>Задача №{row['id']}</b>",
        f"📦 <b>Объект:</b> {html.escape(row['order_title'] or 'без объекта')}",
        f"🔧 <b>Задача:</b> {html.escape(row['title'])}",
    ]
    if row["description"]:
        lines.append(f"📝 <b>Описание:</b> {html.escape(row['description'])}")
    lines.extend(
        [
            f"👤 <b>Ответственный:</b> {html.escape(row['assignee_name'])}",
            f"📅 <b>Срок:</b> {local_dt(row['due_at_utc'], tz)}",
            f"❗ <b>Приоритет:</b> {priorities.get(row['priority'], row['priority'])}",
            f"📊 <b>Статус:</b> {statuses.get(row['status'], row['status'])}",
        ]
    )
    return "\n".join(lines)


def task_actions(task_id: int, status: str, admin: bool) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if status == "new":
        rows.append([("▶️ Взять в работу", f"v3_ts:{task_id}")])
    if status != "done":
        rows.append([("✅ Выполнить", f"v3_td:{task_id}")])
    rows.extend(
        [
            [("💬 Добавить комментарий", f"v3_tcadd:{task_id}"), ("📜 Комментарии", f"v3_tcomments:{task_id}")],
            [("📸 Добавить фото", f"v3_tpadd:{task_id}"), ("🖼 Фотографии", f"v3_tphotos:{task_id}")],
        ]
    )
    if admin:
        rows.append([("🗑 Удалить", f"v3_tdelete:{task_id}")])
    return inline(rows)


def workshop_card(row: aiosqlite.Row, tz: Any) -> str:
    statuses = {"new": "🆕 Новое", "in_progress": "▶️ В работе", "done": "✅ Выполнено"}
    priorities = {"normal": "🟢 Обычный", "high": "🟠 Высокий", "urgent": "🔴 Срочный"}
    due = local_dt(row["due_at_utc"], tz) if int(row["has_due"]) else "без срока"
    repeat = {"none": "нет", "weekly": "каждую неделю", "monthly": "каждый месяц"}.get(row["repeat_rule"], row["repeat_rule"])
    lines = [
        f"📝 <b>Дело мастерской №{row['id']}</b>",
        f"🏷 <b>Категория:</b> {html.escape(row['category'])}",
        f"🔧 <b>Дело:</b> {html.escape(row['title'])}",
    ]
    if row["description"]:
        lines.append(f"📝 <b>Описание:</b> {html.escape(row['description'])}")
    lines.extend(
        [
            f"👤 <b>Ответственный:</b> {html.escape(row['assignee_name'])}",
            f"📅 <b>Срок:</b> {due}",
            f"❗ <b>Приоритет:</b> {priorities.get(row['priority'], row['priority'])}",
            f"📊 <b>Статус:</b> {statuses.get(row['status'], row['status'])}",
        ]
    )
    if row["repeat_rule"] != "none":
        lines.append(f"🔁 <b>Повтор:</b> {repeat}")
    return "\n".join(lines)


def workshop_actions(todo_id: int, status: str, admin: bool) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if status == "new":
        rows.append([("▶️ Взять в работу", f"v3_ws:{todo_id}")])
    if status != "done":
        rows.append([("✅ Выполнить", f"v3_wd:{todo_id}")])
    rows.extend(
        [
            [("💬 Добавить комментарий", f"v3_wcadd:{todo_id}"), ("📜 Комментарии", f"v3_wcomments:{todo_id}")],
            [("📸 Добавить фото", f"v3_wpadd:{todo_id}"), ("🖼 Фотографии", f"v3_wphotos:{todo_id}")],
            [("💰 Добавить расход", f"v3_exp_w:{todo_id}"), ("📊 Расходы по делу", f"v3_wexpenses:{todo_id}")],
        ]
    )
    if admin:
        rows.append([("🗑 Удалить", f"v3_wdelete:{todo_id}")])
    return inline(rows)


# ---------------------------------------------------------------------------
# Состояния
# ---------------------------------------------------------------------------


class NameForm(StatesGroup):
    name = State()


class ObjectForm(StatesGroup):
    category = State()
    custom_category = State()
    title = State()
    confirm = State()
    details = State()
    address = State()
    description = State()
    client_name = State()
    client_phone = State()
    client_telegram = State()
    agreement = State()
    responsible = State()
    due_date = State()
    due_time = State()
    template = State()
    initial_status = State()


class ObjectSearchForm(StatesGroup):
    query = State()


class ObjectEditForm(StatesGroup):
    field = State()
    value = State()


class AgreementForm(StatesGroup):
    text = State()


class CommentForm(StatesGroup):
    text = State()


class PhotoForm(StatesGroup):
    stage = State()
    photos = State()


class TaskAddForm(StatesGroup):
    title = State()
    description = State()
    assignee = State()
    due_date = State()
    due_time = State()
    priority = State()


class EntityCommentForm(StatesGroup):
    text = State()


class EntityPhotoForm(StatesGroup):
    photo = State()


class WorkshopForm(StatesGroup):
    category = State()
    title = State()
    description = State()
    assignee = State()
    due_date = State()
    due_time = State()
    priority = State()
    repeat = State()


class ExpenseFormV3(StatesGroup):
    link_type = State()
    link_id = State()
    category = State()
    amount = State()
    description = State()
    expense_date = State()
    receipt = State()


class ReceiptAddForm(StatesGroup):
    photos = State()


class IncomeFormV3(StatesGroup):
    object_id = State()
    amount = State()
    payment_type = State()
    description = State()
    income_date = State()


class FinanceExportForm(StatesGroup):
    start_date = State()
    end_date = State()


class StaffAddForm(StatesGroup):
    telegram_id = State()
    confirm = State()


class StaffRenameForm(StatesGroup):
    employee = State()
    name = State()


# ---------------------------------------------------------------------------
# Регистрация обработчиков
# ---------------------------------------------------------------------------


def register_extension_handlers(router: Router) -> None:
    @router.message(F.text == "✍️ Указать имя")
    async def v3_name_start(message: Message, state: FSMContext, db: Any) -> None:
        await actor_id(message, db)
        await state.set_state(NameForm.name)
        await message.answer("Введите рабочее имя:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(NameForm.name, F.text)
    async def v3_name_save(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Имя слишком короткое:")
            return
        await db.rename_user(message.from_user.id, name[:100])
        await state.clear()
        from bot import main_keyboard
        await message.answer(
            f"✅ Рабочее имя сохранено: <b>{html.escape(name[:100])}</b>",
            reply_markup=main_keyboard(await is_admin(message.from_user.id, db, settings)),
        )

    # -----------------------------------------------------------------------
    # Объекты
    # -----------------------------------------------------------------------

    @router.message(F.text == "📦 Объекты")
    async def v3_objects_menu(message: Message, db: Any) -> None:
        await actor_id(message, db)
        await message.answer("📦 <b>Объекты</b>", reply_markup=objects_menu())

    @router.message(F.text == "➕ Новый объект")
    async def v3_object_new(message: Message, state: FSMContext, db: Any) -> None:
        await actor_id(message, db)
        await state.clear()
        await state.update_data(
            origin_chat_id=message.chat.id,
            address="",
            description="",
            client_name="",
            client_phone="",
            client_telegram="",
            initial_agreement="",
            responsible_id=None,
            due_at_utc=None,
            use_template=True,
            initial_status="new",
            details_return=False,
        )
        await state.set_state(ObjectForm.category)
        await message.answer("🏷 Выберите категорию объекта:", reply_markup=object_category_markup())

    async def show_object_preview(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        data = await state.get_data()
        if not data.get("category") or not data.get("title"):
            await message.answer("Сначала выберите категорию и введите название объекта.")
            return
        responsible = "не назначен"
        if data.get("responsible_id"):
            row = await db.user_by_id(int(data["responsible_id"]))
            if row:
                responsible = str(row["full_name"])
        status = OBJECT_STATUSES.get(str(data.get("initial_status") or "new"), "🆕 Новый")
        template_count = len(TASK_TEMPLATES.get(str(data["category"]), [])) if data.get("use_template") else 0
        preview = [
            "📦 <b>Новый объект</b>",
            "",
            f"🏷 <b>Категория:</b> {html.escape(str(data['category']))}",
            f"✏️ <b>Название:</b> {html.escape(str(data['title']))}",
            f"📊 <b>Статус:</b> {status}",
            f"👤 <b>Ответственный:</b> {html.escape(responsible)}",
            f"📅 <b>Срок:</b> {local_dt(data.get('due_at_utc'), settings.timezone)}",
            f"📋 <b>Задачи по шаблону:</b> {template_count if template_count else 'нет'}",
        ]
        filled = []
        if data.get("address"):
            filled.append("адрес")
        if data.get("description"):
            filled.append("описание")
        if any(data.get(key) for key in ("client_name", "client_phone", "client_telegram")):
            filled.append("контакты")
        if data.get("initial_agreement"):
            filled.append("договорённость")
        if filled:
            preview.append(f"➕ <b>Подробности:</b> {html.escape(', '.join(filled))}")
        await state.set_state(ObjectForm.confirm)
        await message.answer("\n".join(preview), reply_markup=object_create_markup(data))

    async def show_object_details(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        lines = ["➕ <b>Подробности объекта</b>", "", "Заполните только нужные сведения."]
        if data.get("address"):
            lines.append(f"✅ Адрес: {html.escape(str(data['address']))}")
        if data.get("description"):
            lines.append("✅ Описание добавлено")
        if data.get("client_name"):
            lines.append(f"✅ Заказчик: {html.escape(str(data['client_name']))}")
        if data.get("initial_agreement"):
            lines.append("✅ Договорённость добавлена")
        await state.set_state(ObjectForm.details)
        await message.answer("\n".join(lines), reply_markup=object_details_markup(data))

    async def show_contact_details(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        lines = ["👤 <b>Контакты заказчика</b>"]
        if data.get("client_name"):
            lines.append(f"Имя: {html.escape(str(data['client_name']))}")
        if data.get("client_phone"):
            lines.append(f"Телефон: {html.escape(str(data['client_phone']))}")
        if data.get("client_telegram"):
            lines.append(f"Telegram: {html.escape(str(data['client_telegram']))}")
        await state.set_state(ObjectForm.details)
        await message.answer("\n".join(lines), reply_markup=contact_details_markup(data))

    @router.callback_query(ObjectForm.category, F.data.startswith("v3_oc:"))
    async def v3_object_category(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.split(":", 1)[1]
        if key not in CATEGORY_OPTIONS:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        await callback.answer()
        if key == "other":
            await state.set_state(ObjectForm.custom_category)
            if callback.message:
                await callback.message.answer("Введите свою категорию, например «Лестница» или «Перила»:")
            return
        category = CATEGORY_OPTIONS[key]
        await state.update_data(category=category, use_template=bool(TASK_TEMPLATES.get(category)))
        await state.set_state(ObjectForm.title)
        if callback.message:
            await callback.message.answer("✏️ Введите название объекта:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(ObjectForm.custom_category, F.text)
    async def v3_object_custom_category(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) < 2:
            await message.answer("Введите более понятное название категории:")
            return
        await state.update_data(category=f"📌 {value[:80]}", use_template=False)
        await state.set_state(ObjectForm.title)
        await message.answer("✏️ Введите название объекта:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(ObjectForm.title, F.text)
    async def v3_object_title(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        value = (message.text or "").strip()
        if len(value) < 2:
            await message.answer("Название слишком короткое:")
            return
        await state.update_data(title=value[:200])
        await show_object_preview(message, state, db, settings)

    @router.callback_query(F.data == "v3_opreview")
    async def v3_object_preview_callback(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        await callback.answer()
        if callback.message:
            await show_object_preview(callback.message, state, db, settings)

    @router.callback_query(F.data == "v3_odetails")
    async def v3_object_details(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message:
            await show_object_details(callback.message, state)

    @router.callback_query(F.data.startswith("v3_odetail:"))
    async def v3_object_detail_select(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
        field = callback.data.split(":", 1)[1]
        await callback.answer()
        await state.update_data(details_return=True)
        if not callback.message:
            return
        if field == "address":
            await state.set_state(ObjectForm.address)
            await callback.message.answer("📍 Введите адрес объекта:", reply_markup=kb([["Очистить"], ["❌ Отмена"]]))
        elif field == "description":
            await state.set_state(ObjectForm.description)
            await callback.message.answer("📝 Введите описание объекта:", reply_markup=kb([["Очистить"], ["❌ Отмена"]]))
        elif field == "contacts":
            await show_contact_details(callback.message, state)
        elif field == "agreement":
            await state.set_state(ObjectForm.agreement)
            await callback.message.answer("🤝 Введите первую договорённость с заказчиком:", reply_markup=kb([["Очистить"], ["❌ Отмена"]]))
        elif field == "responsible":
            await state.set_state(ObjectForm.responsible)
            await callback.message.answer("👤 Выберите ответственного:", reply_markup=users_markup(await db.users(), "v3_or", True))
        elif field == "due":
            await state.set_state(ObjectForm.due_date)
            await callback.message.answer("📅 Выберите срок готовности:", reply_markup=date_keyboard(True))
        elif field == "template":
            await state.set_state(ObjectForm.template)
            await callback.message.answer(
                "📋 Создавать стандартные задачи?",
                reply_markup=inline([[("✅ Да, создать", "v3_tpl:yes")], [("⏭ Без задач", "v3_tpl:no")]]),
            )
        elif field == "status":
            await state.set_state(ObjectForm.initial_status)
            await callback.message.answer(
                "📊 Выберите начальный статус:",
                reply_markup=inline([[(label, f"v3_oinitial:{key}")] for key, label in OBJECT_STATUSES.items()]),
            )

    @router.callback_query(F.data.startswith("v3_ocontact:"))
    async def v3_object_contact_select(callback: CallbackQuery, state: FSMContext) -> None:
        field = callback.data.split(":", 1)[1]
        await callback.answer()
        if not callback.message:
            return
        prompts = {
            "name": (ObjectForm.client_name, "👤 Введите имя заказчика:"),
            "phone": (ObjectForm.client_phone, "📞 Введите телефон заказчика:"),
            "telegram": (ObjectForm.client_telegram, "✈️ Введите Telegram заказчика:"),
        }
        target = prompts.get(field)
        if not target:
            return
        await state.set_state(target[0])
        await callback.message.answer(target[1], reply_markup=kb([["Очистить"], ["❌ Отмена"]]))

    @router.message(ObjectForm.address, F.text)
    async def v3_object_address(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(address=value[:300])
        await show_object_details(message, state)

    @router.message(ObjectForm.description, F.text)
    async def v3_object_description(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(description=value[:1500])
        await show_object_details(message, state)

    @router.message(ObjectForm.agreement, F.text)
    async def v3_object_initial_agreement(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(initial_agreement=value[:3000])
        await show_object_details(message, state)

    @router.message(ObjectForm.client_name, F.text)
    async def v3_object_client(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(client_name=value[:200])
        await show_contact_details(message, state)

    @router.message(ObjectForm.client_phone, F.text)
    async def v3_object_phone(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(client_phone=value[:100])
        await show_contact_details(message, state)

    @router.message(ObjectForm.client_telegram, F.text)
    async def v3_object_telegram(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Очистить" else (message.text or "").strip()
        await state.update_data(client_telegram=value[:100])
        await show_contact_details(message, state)

    @router.callback_query(F.data == "v3_oquick_responsible")
    async def v3_object_quick_responsible(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
        await state.update_data(details_return=False)
        await state.set_state(ObjectForm.responsible)
        await callback.answer()
        if callback.message:
            await callback.message.answer("👤 Выберите ответственного:", reply_markup=users_markup(await db.users(), "v3_or", True))

    @router.callback_query(ObjectForm.responsible, F.data.startswith("v3_or:"))
    async def v3_object_responsible(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        value = callback.data.split(":", 1)[1]
        await state.update_data(responsible_id=None if value == "none" else int(value))
        data = await state.get_data()
        await callback.answer()
        if callback.message:
            if data.get("details_return"):
                await show_object_details(callback.message, state)
            else:
                await show_object_preview(callback.message, state, db, settings)

    @router.callback_query(F.data == "v3_oquick_due")
    async def v3_object_quick_due(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(details_return=False)
        await state.set_state(ObjectForm.due_date)
        await callback.answer()
        if callback.message:
            await callback.message.answer("📅 Выберите срок готовности объекта:", reply_markup=date_keyboard(True))

    @router.message(ObjectForm.due_date, F.text)
    async def v3_object_due_date(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        try:
            selected = parse_date_choice(message.text or "", settings.timezone)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        if selected is None:
            await state.update_data(due_at_utc=None)
            data = await state.get_data()
            if data.get("details_return"):
                await show_object_details(message, state)
            else:
                await show_object_preview(message, state, db, settings)
            return
        await state.update_data(due_date=selected.isoformat())
        await state.set_state(ObjectForm.due_time)
        await message.answer("🕐 Выберите время:", reply_markup=time_keyboard())

    @router.message(ObjectForm.due_time, F.text)
    async def v3_object_due_time(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        try:
            clock = parse_time_choice(message.text or "")
            data = await state.get_data()
            due = combine_due(date.fromisoformat(data["due_date"]), clock, settings.timezone)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(due_at_utc=utc_value(due))
        data = await state.get_data()
        if data.get("details_return"):
            await show_object_details(message, state)
        else:
            await show_object_preview(message, state, db, settings)

    @router.callback_query(F.data == "v3_oquick_template")
    async def v3_object_quick_template(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(details_return=False)
        await state.set_state(ObjectForm.template)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "📋 Создавать стандартные задачи?",
                reply_markup=inline([[("✅ Да, создать", "v3_tpl:yes")], [("⏭ Без задач", "v3_tpl:no")]]),
            )

    @router.callback_query(ObjectForm.template, F.data.startswith("v3_tpl:"))
    async def v3_object_template(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        use_template = callback.data.endswith(":yes")
        await state.update_data(use_template=use_template)
        data = await state.get_data()
        await callback.answer()
        if callback.message:
            if data.get("details_return"):
                await show_object_details(callback.message, state)
            else:
                await show_object_preview(callback.message, state, db, settings)

    @router.callback_query(ObjectForm.initial_status, F.data.startswith("v3_oinitial:"))
    async def v3_object_initial_status(callback: CallbackQuery, state: FSMContext) -> None:
        value = callback.data.split(":", 1)[1]
        if value not in OBJECT_STATUSES:
            await callback.answer("Неизвестный статус", show_alert=True)
            return
        await state.update_data(initial_status=value)
        await callback.answer()
        if callback.message:
            await show_object_details(callback.message, state)

    @router.callback_query(F.data == "v3_ocreate")
    async def v3_object_create(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        data = await state.get_data()
        if not data.get("title") or not data.get("category"):
            await callback.answer("Данные создания устарели. Начните заново.", show_alert=True)
            return
        uid = await actor_id(callback, db)
        initial_status = str(data.get("initial_status") or "new")
        general_status = "completed" if initial_status == "completed" else "cancelled" if initial_status == "cancelled" else "active"
        object_id = await db.execute(
            """
            INSERT INTO orders(
                title,client,description,status,created_by,created_at_utc,
                category,address,client_phone,client_telegram,responsible_id,
                due_at_utc,object_status,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["title"], data.get("client_name") or None, data.get("description") or None,
                general_status, uid, now_iso(), data["category"], data.get("address") or None,
                data.get("client_phone") or None, data.get("client_telegram") or None,
                data.get("responsible_id"), data.get("due_at_utc"), initial_status, now_iso(),
            ),
        )
        await db.execute(
            "INSERT INTO object_status_history(order_id,old_status,new_status,changed_by,changed_at_utc) VALUES(?,NULL,?,?,?)",
            (object_id, initial_status, uid, now_iso()),
        )
        if data.get("initial_agreement"):
            await db.execute(
                "INSERT INTO agreements(order_id,text,created_by,created_at_utc,deleted) VALUES(?,?,?,?,0)",
                (object_id, str(data["initial_agreement"])[:3000], uid, now_iso()),
            )
        await db.execute(
            """INSERT INTO object_recent_views(user_id,order_id,viewed_at_utc) VALUES(?,?,?)
               ON CONFLICT(user_id,order_id) DO UPDATE SET viewed_at_utc=excluded.viewed_at_utc""",
            (uid, object_id, now_iso()),
        )
        created_tasks = 0
        category = str(data["category"])
        template = TASK_TEMPLATES.get(category, []) if data.get("use_template") and general_status == "active" else []
        if template:
            template_assignee_id = int(data.get("responsible_id") or uid)
            assignee = await db.user_by_id(template_assignee_id)
            origin_chat = int(data.get("origin_chat_id") or callback.from_user.id)
            now_utc_dt = datetime.now(timezone.utc)
            final_due = dt_from_iso(data["due_at_utc"]) if data.get("due_at_utc") else None
            for index, title in enumerate(template, start=1):
                if final_due and final_due > now_utc_dt + timedelta(hours=1):
                    span = final_due - now_utc_dt
                    due = now_utc_dt + span * (index / len(template))
                else:
                    local_target = datetime.now(settings.timezone) + timedelta(days=index)
                    due = local_target.replace(hour=18, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                tid = await db.create_task(
                    origin_chat, object_id, title, "Создано из шаблона объекта",
                    template_assignee_id, uid, "normal", due,
                )
                targets: set[int] = {callback.from_user.id}
                if assignee:
                    targets.add(int(assignee["telegram_id"]))
                if settings.work_chat_id:
                    targets.add(int(settings.work_chat_id))
                await db.schedule_entity_reminders("task", tid, uid, targets, title, due)
                created_tasks += 1
        await state.clear()
        row = await fetch_object(db, object_id)
        await callback.answer("Объект создан")
        if callback.message and row:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.answer(object_card(row, settings.timezone), reply_markup=object_actions(row, admin))
            suffix = f" Задач из шаблона: {created_tasks}." if created_tasks else ""
            await callback.message.answer(f"✅ Объект создан.{suffix}", reply_markup=objects_menu())

    @router.callback_query(F.data == "v3_cancel")
    async def v3_inline_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.answer("Действие отменено.")

    async def send_compact_objects(
        message: Message,
        rows: Sequence[aiosqlite.Row],
        settings: Any,
        title: str,
    ) -> None:
        if not rows:
            await message.answer("Объекты не найдены.")
            return
        visible = list(rows[:20])
        await message.answer(
            compact_objects_text(visible, settings.timezone, title),
            reply_markup=compact_objects_markup(visible),
        )
        if len(rows) > len(visible):
            await message.answer("Показаны первые 20 объектов. Для остальных используйте поиск.")

    @router.message(F.text.in_({"📋 Все активные", "📋 Активные объекты"}))
    async def v3_objects_active(message: Message, db: Any, settings: Any) -> None:
        await actor_id(message, db)
        await send_compact_objects(message, await list_objects(db, "active"), settings, "📋 Активные объекты")

    @router.message(F.text == "🕓 Недавние объекты")
    async def v3_objects_recent(message: Message, db: Any, settings: Any) -> None:
        uid = await actor_id(message, db)
        rows = await db.fetchall(
            """
            SELECT o.*,c.full_name creator_name,r.full_name responsible_name,
                   (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status<>'done') open_tasks,
                   (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id) total_tasks,
                   (SELECT COUNT(*) FROM tasks t WHERE t.order_id=o.id AND t.status='done') done_tasks,
                   (SELECT COUNT(*) FROM portfolio_photos p WHERE p.order_id=o.id AND p.deleted=0) photo_count
            FROM object_recent_views rv
            JOIN orders o ON o.id=rv.order_id
            JOIN users c ON c.id=o.created_by
            LEFT JOIN users r ON r.id=o.responsible_id
            WHERE rv.user_id=?
            ORDER BY rv.viewed_at_utc DESC
            LIMIT 5
            """,
            (uid,),
        )
        await send_compact_objects(message, rows, settings, "🕓 Недавние объекты")

    @router.message(F.text == "✅ Завершённые объекты")
    async def v3_objects_completed(message: Message, db: Any, settings: Any) -> None:
        await actor_id(message, db)
        await send_compact_objects(message, await list_objects(db, "completed"), settings, "✅ Завершённые объекты")

    @router.message(F.text == "🔍 Найти объект")
    async def v3_object_search_start(message: Message, state: FSMContext, db: Any) -> None:
        await actor_id(message, db)
        await state.set_state(ObjectSearchForm.query)
        await message.answer("Введите название, адрес, имя клиента или телефон:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(ObjectSearchForm.query, F.text)
    async def v3_object_search(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        query = (message.text or "").strip()
        await state.clear()
        await send_compact_objects(message, await list_objects(db, "all", query), settings, f"🔍 Результаты поиска: {query}")

    @router.callback_query(F.data.startswith("v3_openobj:"))
    async def v3_open_object(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        row = await fetch_object(db, oid)
        if not row:
            await callback.answer("Объект не найден", show_alert=True)
            return
        uid = await actor_id(callback, db)
        await db.execute(
            """INSERT INTO object_recent_views(user_id,order_id,viewed_at_utc) VALUES(?,?,?)
               ON CONFLICT(user_id,order_id) DO UPDATE SET viewed_at_utc=excluded.viewed_at_utc""",
            (uid, oid, now_iso()),
        )
        await callback.answer()
        if callback.message:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.answer(object_card(row, settings.timezone), reply_markup=object_actions(row, admin))

    @router.callback_query(F.data.startswith("v3_ostatus:"))
    async def v3_object_status_menu(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        row = await fetch_object(db, oid)
        if not row:
            await callback.answer("Объект не найден", show_alert=True)
            return
        allowed = await is_admin(callback.from_user.id, db, settings) or int(row["responsible_telegram_id"] or 0) == callback.from_user.id
        if not allowed:
            await callback.answer("Статус меняет ответственный или администратор", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите новый статус:", reply_markup=object_status_markup(oid))

    @router.callback_query(F.data.startswith("v3_oset:"))
    async def v3_object_status_set(callback: CallbackQuery, db: Any, settings: Any) -> None:
        _, oid_text, new_status = callback.data.split(":", 2)
        oid = int(oid_text)
        if new_status not in OBJECT_STATUSES:
            await callback.answer("Неизвестный статус", show_alert=True)
            return
        row = await fetch_object(db, oid)
        if not row:
            await callback.answer("Объект не найден", show_alert=True)
            return
        allowed = await is_admin(callback.from_user.id, db, settings) or int(row["responsible_telegram_id"] or 0) == callback.from_user.id
        if not allowed:
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        uid = await actor_id(callback, db)
        old = str(row["object_status"])
        general_status = "completed" if new_status == "completed" else "cancelled" if new_status == "cancelled" else "active"
        await db.execute(
            "UPDATE orders SET object_status=?,status=?,updated_at_utc=? WHERE id=?",
            (new_status, general_status, now_iso(), oid),
        )
        await db.execute(
            "INSERT INTO object_status_history(order_id,old_status,new_status,changed_by,changed_at_utc) VALUES(?,?,?,?,?)",
            (oid, old, new_status, uid, now_iso()),
        )
        if new_status in {"completed", "cancelled"}:
            await db.execute(
                """UPDATE reminders SET cancelled=1
                   WHERE entity_type='task'
                     AND entity_id IN (SELECT id FROM tasks WHERE order_id=?)
                     AND sent_at_utc IS NULL""",
                (oid,),
            )
        updated = await fetch_object(db, oid)
        await callback.answer("Статус изменён")
        if callback.message and updated:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.answer(object_card(updated, settings.timezone), reply_markup=object_actions(updated, admin))

    @router.callback_query(F.data.startswith("v3_contacts:"))
    async def v3_contacts(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        row = await fetch_object(db, oid)
        if not row:
            await callback.answer("Объект не найден", show_alert=True)
            return
        admin = await is_admin(callback.from_user.id, db, settings)
        lines = [f"👤 <b>Заказчик:</b> {html.escape(row['client'] or 'не указан')}"]
        if admin:
            lines.extend(
                [
                    f"📞 <b>Телефон:</b> {html.escape(row['client_phone'] or '—')}",
                    f"✈️ <b>Telegram:</b> {html.escape(row['client_telegram'] or '—')}",
                    f"📍 <b>Адрес:</b> {html.escape(row['address'] or '—')}",
                ]
            )
        else:
            lines.append("Полные контакты доступны администраторам.")
        await callback.answer()
        if callback.message:
            await callback.message.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("v3_agreements:"))
    async def v3_agreements(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall(
            """
            SELECT a.*,u.full_name creator_name FROM agreements a
            JOIN users u ON u.id=a.created_by
            WHERE a.order_id=? AND a.deleted=0 ORDER BY a.id DESC LIMIT 30
            """,
            (oid,),
        )
        admin = await is_admin(callback.from_user.id, db, settings)
        await callback.answer()
        if not callback.message:
            return
        if rows:
            lines = ["🤝 <b>Договорённости</b>"]
            for row in reversed(rows):
                when = dt_from_iso(row["created_at_utc"]).astimezone(settings.timezone).strftime("%d.%m %H:%M")
                lines.append(f"\n{when} — {html.escape(row['text'])}\n<i>{html.escape(row['creator_name'])}</i>")
            await callback.message.answer("\n".join(lines))
        else:
            await callback.message.answer("Договорённостей пока нет.")
        if admin:
            await callback.message.answer(
                "Добавить важное решение клиента:",
                reply_markup=inline([[('➕ Добавить договорённость', f'v3_agree_add:{oid}')]]),
            )

    @router.callback_query(F.data.startswith("v3_agree_add:"))
    async def v3_agreement_add_start(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        oid = int(callback.data.split(":", 1)[1])
        await state.update_data(object_id=oid)
        await state.set_state(AgreementForm.text)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите договорённость с заказчиком:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(AgreementForm.text, F.text)
    async def v3_agreement_add(message: Message, state: FSMContext, db: Any) -> None:
        text = (message.text or "").strip()
        if len(text) < 3:
            await message.answer("Введите более подробный текст:")
            return
        uid = await actor_id(message, db)
        data = await state.get_data()
        aid = await db.execute(
            "INSERT INTO agreements(order_id,text,created_by,created_at_utc) VALUES(?,?,?,?)",
            (int(data["object_id"]), text[:3000], uid, now_iso()),
        )
        await state.clear()
        await message.answer(f"✅ Договорённость №{aid} сохранена.", reply_markup=objects_menu())

    @router.callback_query(F.data.startswith("v3_ocomments:"))
    async def v3_object_comments(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall(
            """SELECT c.*,u.full_name creator_name FROM order_comments c
               JOIN users u ON u.id=c.created_by
               WHERE c.order_id=? AND c.deleted=0 ORDER BY c.id DESC LIMIT 30""",
            (oid,),
        )
        await callback.answer()
        if not callback.message:
            return
        if rows:
            for row in reversed(rows):
                when = dt_from_iso(row["created_at_utc"]).astimezone(settings.timezone).strftime("%d.%m %H:%M")
                await callback.message.answer(f"💬 {html.escape(row['text'])}\n<i>{html.escape(row['creator_name'])}, {when}</i>")
        else:
            await callback.message.answer("Комментариев пока нет.")
        await callback.message.answer("Добавить комментарий:", reply_markup=inline([[('💬 Добавить', f'v3_ocadd:{oid}')]]))

    @router.callback_query(F.data.startswith("v3_ocadd:"))
    async def v3_object_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(object_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(CommentForm.text)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите рабочий комментарий:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(CommentForm.text, F.text)
    async def v3_object_comment_save(message: Message, state: FSMContext, db: Any) -> None:
        text = (message.text or "").strip()
        if len(text) < 2:
            await message.answer("Комментарий слишком короткий:")
            return
        uid = await actor_id(message, db)
        data = await state.get_data()
        cid = await db.execute(
            "INSERT INTO order_comments(order_id,text,created_by,created_at_utc) VALUES(?,?,?,?)",
            (int(data["object_id"]), text[:3000], uid, now_iso()),
        )
        await state.clear()
        await message.answer(f"✅ Комментарий №{cid} добавлен.", reply_markup=objects_menu())

    # -----------------------------------------------------------------------
    # Задачи объекта и «Мои задачи»
    # -----------------------------------------------------------------------

    @router.message(F.text == "✅ Мои задачи")
    @router.message(Command("tasks"))
    async def v3_tasks_menu(message: Message, db: Any) -> None:
        await actor_id(message, db)
        await message.answer("✅ <b>Мои задачи</b>", reply_markup=my_tasks_menu())

    @router.callback_query(F.data.startswith("v3_tadd:"))
    async def v3_task_add_start(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        if not await fetch_object(db, oid):
            await callback.answer("Объект не найден", show_alert=True)
            return
        await state.clear()
        await state.update_data(object_id=oid, origin_chat_id=callback.message.chat.id if callback.message else callback.from_user.id)
        await state.set_state(TaskAddForm.title)
        await callback.answer()
        if callback.message:
            await callback.message.answer("🔧 Введите название задачи:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(TaskAddForm.title, F.text)
    async def v3_task_title(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) < 2:
            await message.answer("Опишите задачу подробнее:")
            return
        await state.update_data(title=value[:300])
        await state.set_state(TaskAddForm.description)
        await message.answer("Добавьте описание или нажмите «Пропустить»:", reply_markup=kb([["Пропустить"], ["❌ Отмена"]]))

    @router.message(TaskAddForm.description, F.text)
    async def v3_task_description(message: Message, state: FSMContext, db: Any) -> None:
        value = "" if message.text == "Пропустить" else (message.text or "").strip()
        await state.update_data(description=value[:1500])
        await state.set_state(TaskAddForm.assignee)
        await message.answer("Выберите ответственного:", reply_markup=users_markup(await db.users(), "v3_ta"))

    @router.callback_query(TaskAddForm.assignee, F.data.startswith("v3_ta:"))
    async def v3_task_assignee(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(assignee_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(TaskAddForm.due_date)
        await callback.answer()
        if callback.message:
            await callback.message.answer("📅 Выберите срок задачи:", reply_markup=date_keyboard(False))

    @router.message(TaskAddForm.due_date, F.text)
    async def v3_task_due_date(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            selected = parse_date_choice(message.text or "", settings.timezone)
            if selected is None:
                raise ValueError("Для задачи нужен срок")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(due_date=selected.isoformat())
        await state.set_state(TaskAddForm.due_time)
        await message.answer("🕐 Выберите время:", reply_markup=time_keyboard())

    @router.message(TaskAddForm.due_time, F.text)
    async def v3_task_due_time(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            clock = parse_time_choice(message.text or "")
            data = await state.get_data()
            due = combine_due(date.fromisoformat(data["due_date"]), clock, settings.timezone)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(due_at_utc=utc_value(due))
        await state.set_state(TaskAddForm.priority)
        await message.answer(
            "❗ Выберите приоритет:",
            reply_markup=inline(
                [
                    [("🟢 Обычный", "v3_tpr:normal"), ("🟠 Высокий", "v3_tpr:high")],
                    [("🔴 Срочный", "v3_tpr:urgent")],
                ]
            ),
        )

    @router.callback_query(TaskAddForm.priority, F.data.startswith("v3_tpr:"))
    async def v3_task_create(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        priority = callback.data.split(":", 1)[1]
        uid = await actor_id(callback, db)
        data = await state.get_data()
        due = dt_from_iso(data["due_at_utc"])
        tid = await db.create_task(
            int(data["origin_chat_id"]), int(data["object_id"]), data["title"], data.get("description", ""),
            int(data["assignee_id"]), uid, priority, due,
        )
        assignee = await db.user_by_id(int(data["assignee_id"]))
        targets = {callback.from_user.id}
        if assignee:
            targets.add(int(assignee["telegram_id"]))
        if settings.work_chat_id:
            targets.add(int(settings.work_chat_id))
        await db.schedule_entity_reminders("task", tid, uid, targets, data["title"], due)
        await state.clear()
        row = await db.task_by_id(tid)
        await callback.answer("Задача создана")
        if callback.message and row:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.answer(task_card(row, settings.timezone), reply_markup=task_actions(tid, row["status"], admin))

    async def send_tasks(message: Message, db: Any, settings: Any, mode: str, object_id: int | None = None, viewer_id: int | None = None) -> None:
        tz = settings.timezone
        now = datetime.now(timezone.utc)
        params: list[Any] = []
        where: list[str] = []
        effective_user_id = viewer_id if viewer_id is not None else message.from_user.id
        if object_id is None:
            where.append("a.telegram_id=?")
            params.append(effective_user_id)
        else:
            where.append("t.order_id=?")
            params.append(object_id)
        if object_id is None and mode != "done":
            where.append("(o.status='active' OR o.id IS NULL)")
        if mode == "today":
            start_local = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(timezone.utc)
            end_local = start_local + timedelta(days=1)
            where += ["t.status<>'done'", "t.due_at_utc>=?", "t.due_at_utc<?"]
            params += [utc_value(start_local), utc_value(end_local)]
        elif mode == "upcoming":
            where += ["t.status<>'done'", "t.due_at_utc>=?"]
            params.append(utc_value(now))
        elif mode == "overdue":
            where += ["t.status<>'done'", "t.due_at_utc<?"]
            params.append(utc_value(now))
        elif mode == "in_progress":
            where.append("t.status='in_progress'")
        elif mode == "done":
            where.append("t.status='done'")
        else:
            where.append("1=1")
        rows = await db.fetchall(
            f"""
            SELECT t.*,a.full_name assignee_name,a.telegram_id assignee_telegram_id,
                   c.full_name creator_name,c.telegram_id creator_telegram_id,o.title order_title
            FROM tasks t JOIN users a ON a.id=t.assignee_id JOIN users c ON c.id=t.creator_id
            LEFT JOIN orders o ON o.id=t.order_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE WHEN t.status='done' THEN t.completed_at_utc ELSE t.due_at_utc END DESC
            LIMIT 50
            """,
            params,
        )
        if not rows:
            await message.answer("Задач в этом разделе нет.")
            return
        admin = await is_admin(effective_user_id, db, settings)
        for row in rows:
            await message.answer(task_card(row, tz), reply_markup=task_actions(int(row["id"]), row["status"], admin))

    @router.message(F.text == "🔥 На сегодня")
    async def v3_tasks_today(message: Message, db: Any, settings: Any) -> None:
        await send_tasks(message, db, settings, "today")

    @router.message(F.text == "📅 Ближайшие")
    async def v3_tasks_upcoming(message: Message, db: Any, settings: Any) -> None:
        await send_tasks(message, db, settings, "upcoming")

    @router.message(F.text == "⚠️ Просроченные")
    async def v3_tasks_overdue(message: Message, db: Any, settings: Any) -> None:
        await send_tasks(message, db, settings, "overdue")

    @router.message(F.text == "▶️ В работе")
    async def v3_tasks_progress(message: Message, db: Any, settings: Any) -> None:
        await send_tasks(message, db, settings, "in_progress")

    @router.message(F.text == "✅ Выполненные задачи")
    async def v3_tasks_done(message: Message, db: Any, settings: Any) -> None:
        await send_tasks(message, db, settings, "done")

    @router.callback_query(F.data.startswith("v3_otasks:"))
    async def v3_object_tasks(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        await callback.answer()
        if callback.message:
            await send_tasks(callback.message, db, settings, "all", oid, callback.from_user.id)

    async def can_manage_task(row: aiosqlite.Row, user_id: int, db: Any, settings: Any) -> bool:
        return user_id in {int(row["assignee_telegram_id"]), int(row["creator_telegram_id"])} or await is_admin(user_id, db, settings)

    @router.callback_query(F.data.startswith("v3_ts:"))
    async def v3_task_start(callback: CallbackQuery, db: Any, settings: Any) -> None:
        tid = int(callback.data.split(":", 1)[1])
        row = await db.task_by_id(tid)
        if not row or not await can_manage_task(row, callback.from_user.id, db, settings):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await db.set_task_status(tid, "in_progress")
        updated = await db.task_by_id(tid)
        await callback.answer("Задача взята в работу")
        if callback.message and updated:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.edit_text(task_card(updated, settings.timezone), reply_markup=task_actions(tid, updated["status"], admin))

    @router.callback_query(F.data.startswith("v3_td:"))
    async def v3_task_done(callback: CallbackQuery, db: Any, settings: Any) -> None:
        tid = int(callback.data.split(":", 1)[1])
        row = await db.task_by_id(tid)
        if not row or not await can_manage_task(row, callback.from_user.id, db, settings):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await db.set_task_status(tid, "done")
        updated = await db.task_by_id(tid)
        await callback.answer("Задача выполнена")
        if callback.message and updated:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.edit_text(task_card(updated, settings.timezone), reply_markup=task_actions(tid, updated["status"], admin))
            title = str(row["title"]).lower()
            suggestion = None
            if "замер" in title:
                suggestion = "design"
            elif "изготов" in title:
                suggestion = "painting"
            elif "монтаж" in title or "итогов" in title:
                suggestion = "completed"
            if suggestion and row["order_id"]:
                await callback.message.answer(
                    f"Изменить статус объекта на «{OBJECT_STATUSES[suggestion]}»?",
                    reply_markup=inline(
                        [[("✅ Да", f"v3_oset:{row['order_id']}:{suggestion}"), ("⏭ Оставить", "noop")]]
                    ),
                )

    @router.callback_query(F.data.startswith("v3_tdelete:"))
    async def v3_task_delete(callback: CallbackQuery, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        tid = int(callback.data.split(":", 1)[1])
        await db.cancel_entity_reminders("task", tid)
        await db.execute("DELETE FROM task_comments WHERE task_id=?", (tid,))
        await db.execute("DELETE FROM task_photos WHERE task_id=?", (tid,))
        await db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        await callback.answer("Задача удалена")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    # Комментарии и фото задач
    @router.callback_query(F.data.startswith("v3_tcadd:"))
    async def v3_task_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(entity_type="task", entity_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(EntityCommentForm.text)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите комментарий к задаче:", reply_markup=kb([["❌ Отмена"]]))

    @router.callback_query(F.data.startswith("v3_tcomments:"))
    async def v3_task_comments(callback: CallbackQuery, db: Any, settings: Any) -> None:
        tid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall(
            """SELECT c.*,u.full_name creator_name FROM task_comments c JOIN users u ON u.id=c.created_by
               WHERE c.task_id=? AND c.deleted=0 ORDER BY c.id""", (tid,)
        )
        await callback.answer()
        if callback.message:
            if not rows:
                await callback.message.answer("Комментариев пока нет.")
            for row in rows:
                when = dt_from_iso(row["created_at_utc"]).astimezone(settings.timezone).strftime("%d.%m %H:%M")
                await callback.message.answer(f"💬 {html.escape(row['text'])}\n<i>{html.escape(row['creator_name'])}, {when}</i>")

    @router.callback_query(F.data.startswith("v3_tpadd:"))
    async def v3_task_photo_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(entity_type="task", entity_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(EntityPhotoForm.photo)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Отправьте фотографию к задаче:", reply_markup=kb([["❌ Отмена"]]))

    @router.callback_query(F.data.startswith("v3_tphotos:"))
    async def v3_task_photos(callback: CallbackQuery, db: Any, settings: Any) -> None:
        tid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall(
            """SELECT p.*,u.full_name creator_name FROM task_photos p JOIN users u ON u.id=p.created_by
               WHERE p.task_id=? AND p.deleted=0 ORDER BY p.id""", (tid,)
        )
        await callback.answer()
        if callback.message:
            if not rows:
                await callback.message.answer("Фотографий пока нет.")
            for row in rows[-20:]:
                await callback.message.answer_photo(row["telegram_file_id"], caption=html.escape(row["caption"] or f"Фото задачи №{tid}"))

    @router.message(EntityCommentForm.text, F.text)
    async def v3_entity_comment_save(message: Message, state: FSMContext, db: Any) -> None:
        text = (message.text or "").strip()
        if len(text) < 2:
            await message.answer("Комментарий слишком короткий:")
            return
        data = await state.get_data()
        uid = await actor_id(message, db)
        table = "task_comments" if data["entity_type"] == "task" else "todo_comments"
        id_col = "task_id" if data["entity_type"] == "task" else "todo_id"
        cid = await db.execute(
            f"INSERT INTO {table}({id_col},text,created_by,created_at_utc) VALUES(?,?,?,?)",
            (int(data["entity_id"]), text[:3000], uid, now_iso()),
        )
        await state.clear()
        await message.answer(f"✅ Комментарий №{cid} добавлен.")

    @router.message(EntityPhotoForm.photo, F.photo)
    async def v3_entity_photo_save(message: Message, state: FSMContext, db: Any) -> None:
        data = await state.get_data()
        uid = await actor_id(message, db)
        table = "task_photos" if data["entity_type"] == "task" else "todo_photos"
        id_col = "task_id" if data["entity_type"] == "task" else "todo_id"
        caption = (message.caption or "")[:1000] or None
        pid = await db.execute(
            f"INSERT INTO {table}({id_col},telegram_file_id,caption,created_by,created_at_utc) VALUES(?,?,?,?,?)",
            (int(data["entity_id"]), message.photo[-1].file_id, caption, uid, now_iso()),
        )
        portfolio_note = ""
        if data["entity_type"] == "task":
            task = await db.task_by_id(int(data["entity_id"]))
            if task and task["order_id"]:
                obj = await fetch_object(db, int(task["order_id"]))
                status = str(obj["object_status"]) if obj else "manufacturing"
                stage = {
                    "measurement": "measurement",
                    "design": "manufacturing",
                    "manufacturing": "manufacturing",
                    "painting": "manufacturing",
                    "installation": "installation",
                    "completed": "finished",
                }.get(status, "manufacturing")
                await db.execute(
                    """INSERT INTO portfolio_photos(
                           order_id,stage,telegram_file_id,telegram_file_unique_id,local_path,
                           caption,created_by,created_at_utc
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (int(task["order_id"]), stage, message.photo[-1].file_id,
                     message.photo[-1].file_unique_id, None, caption, uid, now_iso()),
                )
                portfolio_note = " Фото также добавлено в фотографии объекта и портфолио."
        await state.clear()
        await message.answer(f"✅ Фото №{pid} сохранено.{portfolio_note}")

    @router.message(EntityPhotoForm.photo)
    async def v3_entity_photo_wrong(message: Message) -> None:
        await message.answer("Нужно отправить фотографию.")

    # -----------------------------------------------------------------------
    # Дела мастерской
    # -----------------------------------------------------------------------

    @router.message(F.text == "📝 Дела мастерской")
    @router.message(Command("todos"))
    async def v3_workshop_menu(message: Message, db: Any) -> None:
        await actor_id(message, db)
        await message.answer("📝 <b>Дела мастерской</b>", reply_markup=workshop_menu())

    @router.message(F.text == "➕ Новое дело")
    async def v3_workshop_new(message: Message, state: FSMContext, db: Any) -> None:
        await actor_id(message, db)
        await state.clear()
        await state.update_data(origin_chat_id=message.chat.id)
        await state.set_state(WorkshopForm.category)
        await message.answer(
            "Выберите категорию:",
            reply_markup=inline([[(label, f"v3_wc:{key}")] for key, label in WORKSHOP_CATEGORIES.items()]),
        )

    @router.callback_query(WorkshopForm.category, F.data.startswith("v3_wc:"))
    async def v3_workshop_category(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.split(":", 1)[1]
        await state.update_data(category=WORKSHOP_CATEGORIES[key])
        await state.set_state(WorkshopForm.title)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Что нужно сделать в мастерской?", reply_markup=kb([["❌ Отмена"]]))

    @router.message(WorkshopForm.title, F.text)
    async def v3_workshop_title(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) < 2:
            await message.answer("Опишите дело подробнее:")
            return
        await state.update_data(title=value[:300])
        await state.set_state(WorkshopForm.description)
        await message.answer("Добавьте описание или нажмите «Пропустить»:", reply_markup=kb([["Пропустить"], ["❌ Отмена"]]))

    @router.message(WorkshopForm.description, F.text)
    async def v3_workshop_description(message: Message, state: FSMContext, db: Any) -> None:
        value = "" if message.text == "Пропустить" else (message.text or "").strip()
        await state.update_data(description=value[:1500])
        await state.set_state(WorkshopForm.assignee)
        await message.answer("Выберите ответственного:", reply_markup=users_markup(await db.users(), "v3_wa"))

    @router.callback_query(WorkshopForm.assignee, F.data.startswith("v3_wa:"))
    async def v3_workshop_assignee(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(assignee_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(WorkshopForm.due_date)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите срок:", reply_markup=date_keyboard(True))

    @router.message(WorkshopForm.due_date, F.text)
    async def v3_workshop_due_date(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            selected = parse_date_choice(message.text or "", settings.timezone)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        if selected is None:
            await state.update_data(has_due=0, due_at_utc=utc_value(datetime(2099, 1, 1, tzinfo=timezone.utc)))
            await state.set_state(WorkshopForm.priority)
            await message.answer(
                "Выберите приоритет:",
                reply_markup=inline(
                    [[("🟢 Обычный", "v3_wpr:normal"), ("🟠 Высокий", "v3_wpr:high")], [("🔴 Срочный", "v3_wpr:urgent")]]
                ),
            )
            return
        await state.update_data(has_due=1, due_date=selected.isoformat())
        await state.set_state(WorkshopForm.due_time)
        await message.answer("Выберите время:", reply_markup=time_keyboard())

    @router.message(WorkshopForm.due_time, F.text)
    async def v3_workshop_due_time(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            clock = parse_time_choice(message.text or "")
            data = await state.get_data()
            due = combine_due(date.fromisoformat(data["due_date"]), clock, settings.timezone)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(due_at_utc=utc_value(due))
        await state.set_state(WorkshopForm.priority)
        await message.answer(
            "Выберите приоритет:",
            reply_markup=inline(
                [[("🟢 Обычный", "v3_wpr:normal"), ("🟠 Высокий", "v3_wpr:high")], [("🔴 Срочный", "v3_wpr:urgent")]]
            ),
        )

    @router.callback_query(WorkshopForm.priority, F.data.startswith("v3_wpr:"))
    async def v3_workshop_priority(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(priority=callback.data.split(":", 1)[1])
        await state.set_state(WorkshopForm.repeat)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Повторять это дело?",
                reply_markup=inline(
                    [
                        [("Нет", "v3_wr:none")],
                        [("🔁 Каждую неделю", "v3_wr:weekly")],
                        [("🔁 Каждый месяц", "v3_wr:monthly")],
                    ]
                ),
            )

    @router.callback_query(WorkshopForm.repeat, F.data.startswith("v3_wr:"))
    async def v3_workshop_create(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        repeat_rule = callback.data.split(":", 1)[1]
        uid = await actor_id(callback, db)
        data = await state.get_data()
        due = dt_from_iso(data["due_at_utc"])
        wid = await db.create_todo(
            int(data["origin_chat_id"]), data["title"], int(data["assignee_id"]), uid, data["priority"], due
        )
        await db.execute(
            "UPDATE todos SET category=?,description=?,repeat_rule=?,has_due=? WHERE id=?",
            (data["category"], data.get("description") or None, repeat_rule, int(data.get("has_due", 1)), wid),
        )
        assignee = await db.user_by_id(int(data["assignee_id"]))
        if int(data.get("has_due", 1)):
            targets = {callback.from_user.id}
            if assignee:
                targets.add(int(assignee["telegram_id"]))
            if settings.work_chat_id:
                targets.add(int(settings.work_chat_id))
            await db.schedule_entity_reminders("todo", wid, uid, targets, data["title"], due)
        await state.clear()
        row = await db.todo_by_id(wid)
        await callback.answer("Дело создано")
        if callback.message and row:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.answer(workshop_card(row, settings.timezone), reply_markup=workshop_actions(wid, row["status"], admin))
            await callback.message.answer("✅ Дело мастерской создано.", reply_markup=workshop_menu())

    async def send_workshop(message: Message, db: Any, settings: Any, mode: str) -> None:
        where: list[str] = []
        params: list[Any] = []
        now = datetime.now(timezone.utc)
        if mode == "active":
            where.append("t.status<>'done'")
        elif mode == "mine":
            where += ["t.status<>'done'", "a.telegram_id=?"]
            params.append(message.from_user.id)
        elif mode == "overdue":
            where += ["t.status<>'done'", "t.has_due=1", "t.due_at_utc<?"]
            params.append(utc_value(now))
        elif mode == "done":
            where.append("t.status='done'")
        elif mode == "regular":
            where += ["t.status<>'done'", "t.repeat_rule<>'none'"]
        rows = await db.fetchall(
            f"""SELECT t.*,a.full_name assignee_name,a.telegram_id assignee_telegram_id,
                       c.full_name creator_name,c.telegram_id creator_telegram_id
                FROM todos t JOIN users a ON a.id=t.assignee_id JOIN users c ON c.id=t.creator_id
                WHERE {' AND '.join(where)} ORDER BY t.due_at_utc LIMIT 50""",
            params,
        )
        if not rows:
            await message.answer("Дел в этом разделе нет.")
            return
        admin = await is_admin(message.from_user.id, db, settings)
        for row in rows:
            await message.answer(workshop_card(row, settings.timezone), reply_markup=workshop_actions(int(row["id"]), row["status"], admin))

    @router.message(F.text == "📋 Активные дела")
    async def v3_workshop_active(message: Message, db: Any, settings: Any) -> None:
        await send_workshop(message, db, settings, "active")

    @router.message(F.text == "👤 Мои дела")
    async def v3_workshop_mine(message: Message, db: Any, settings: Any) -> None:
        await send_workshop(message, db, settings, "mine")

    @router.message(F.text == "⚠️ Просроченные дела")
    async def v3_workshop_overdue(message: Message, db: Any, settings: Any) -> None:
        await send_workshop(message, db, settings, "overdue")

    @router.message(F.text == "✅ Выполненные дела")
    async def v3_workshop_done_list(message: Message, db: Any, settings: Any) -> None:
        await send_workshop(message, db, settings, "done")

    @router.message(F.text == "🔁 Регулярные дела")
    async def v3_workshop_regular(message: Message, db: Any, settings: Any) -> None:
        await send_workshop(message, db, settings, "regular")

    async def can_manage_workshop(row: aiosqlite.Row, user_id: int, db: Any, settings: Any) -> bool:
        return user_id in {int(row["assignee_telegram_id"]), int(row["creator_telegram_id"])} or await is_admin(user_id, db, settings)

    @router.callback_query(F.data.startswith("v3_ws:"))
    async def v3_workshop_start(callback: CallbackQuery, db: Any, settings: Any) -> None:
        wid = int(callback.data.split(":", 1)[1])
        row = await db.todo_by_id(wid)
        if not row or not await can_manage_workshop(row, callback.from_user.id, db, settings):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await db.set_todo_status(wid, "in_progress")
        updated = await db.todo_by_id(wid)
        await callback.answer("Дело взято в работу")
        if callback.message and updated:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.edit_text(workshop_card(updated, settings.timezone), reply_markup=workshop_actions(wid, updated["status"], admin))

    @router.callback_query(F.data.startswith("v3_wd:"))
    async def v3_workshop_done(callback: CallbackQuery, db: Any, settings: Any) -> None:
        wid = int(callback.data.split(":", 1)[1])
        row = await db.todo_by_id(wid)
        if not row or not await can_manage_workshop(row, callback.from_user.id, db, settings):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        await db.set_todo_status(wid, "done")
        # Для регулярного дела создаём следующий экземпляр.
        if row["repeat_rule"] in {"weekly", "monthly"}:
            old_due = dt_from_iso(row["due_at_utc"])
            next_due = old_due + (timedelta(days=7) if row["repeat_rule"] == "weekly" else timedelta(days=30))
            new_id = await db.create_todo(
                int(row["chat_id"]), row["title"], int(row["assignee_id"]), int(row["creator_id"]),
                row["priority"], next_due,
            )
            await db.execute(
                "UPDATE todos SET category=?,description=?,repeat_rule=?,has_due=? WHERE id=?",
                (row["category"], row["description"], row["repeat_rule"], row["has_due"], new_id),
            )
            if int(row["has_due"]):
                await db.schedule_entity_reminders(
                    "todo", new_id, int(row["creator_id"]),
                    {int(row["assignee_telegram_id"]), int(row["creator_telegram_id"])}, row["title"], next_due,
                )
        updated = await db.todo_by_id(wid)
        await callback.answer("Дело выполнено")
        if callback.message and updated:
            admin = await is_admin(callback.from_user.id, db, settings)
            await callback.message.edit_text(workshop_card(updated, settings.timezone), reply_markup=workshop_actions(wid, updated["status"], admin))

    @router.callback_query(F.data.startswith("v3_wdelete:"))
    async def v3_workshop_delete(callback: CallbackQuery, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        wid = int(callback.data.split(":", 1)[1])
        await db.cancel_entity_reminders("todo", wid)
        await db.execute("DELETE FROM todo_comments WHERE todo_id=?", (wid,))
        await db.execute("DELETE FROM todo_photos WHERE todo_id=?", (wid,))
        await db.execute("UPDATE expenses SET todo_id=NULL WHERE todo_id=?", (wid,))
        await db.execute("DELETE FROM todos WHERE id=?", (wid,))
        await callback.answer("Дело удалено")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("v3_wcadd:"))
    async def v3_workshop_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(entity_type="todo", entity_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(EntityCommentForm.text)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите комментарий к делу:", reply_markup=kb([["❌ Отмена"]]))

    @router.callback_query(F.data.startswith("v3_wcomments:"))
    async def v3_workshop_comments(callback: CallbackQuery, db: Any, settings: Any) -> None:
        wid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall(
            """SELECT c.*,u.full_name creator_name FROM todo_comments c JOIN users u ON u.id=c.created_by
               WHERE c.todo_id=? AND c.deleted=0 ORDER BY c.id""", (wid,)
        )
        await callback.answer()
        if callback.message:
            if not rows:
                await callback.message.answer("Комментариев пока нет.")
            for row in rows:
                when = dt_from_iso(row["created_at_utc"]).astimezone(settings.timezone).strftime("%d.%m %H:%M")
                await callback.message.answer(f"💬 {html.escape(row['text'])}\n<i>{html.escape(row['creator_name'])}, {when}</i>")

    @router.callback_query(F.data.startswith("v3_wpadd:"))
    async def v3_workshop_photo_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(entity_type="todo", entity_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(EntityPhotoForm.photo)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Отправьте фотографию к делу:", reply_markup=kb([["❌ Отмена"]]))

    @router.callback_query(F.data.startswith("v3_wphotos:"))
    async def v3_workshop_photos(callback: CallbackQuery, db: Any) -> None:
        wid = int(callback.data.split(":", 1)[1])
        rows = await db.fetchall("SELECT * FROM todo_photos WHERE todo_id=? AND deleted=0 ORDER BY id", (wid,))
        await callback.answer()
        if callback.message:
            if not rows:
                await callback.message.answer("Фотографий пока нет.")
            for row in rows[-20:]:
                await callback.message.answer_photo(row["telegram_file_id"], caption=html.escape(row["caption"] or f"Фото дела №{wid}"))

    # -----------------------------------------------------------------------
    # Портфолио и фотографии объектов
    # -----------------------------------------------------------------------

    @router.message(F.text == "📸 Портфолио")
    @router.message(Command("portfolio"))
    async def v3_portfolio_menu(message: Message, db: Any) -> None:
        await actor_id(message, db)
        await message.answer("📸 <b>Портфолио</b>", reply_markup=portfolio_menu())

    @router.callback_query(F.data.startswith("v3_padd:"))
    async def v3_photo_add_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.update_data(object_id=int(callback.data.split(":", 1)[1]), photo_count=0)
        await state.set_state(PhotoForm.stage)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите этап фотографии:", reply_markup=stages_markup("v3_ps"))

    @router.callback_query(PhotoForm.stage, F.data.startswith("v3_ps:"))
    async def v3_photo_stage(callback: CallbackQuery, state: FSMContext) -> None:
        stage = callback.data.split(":", 1)[1]
        if stage not in PHOTO_STAGES:
            await callback.answer("Неизвестный этап", show_alert=True)
            return
        await state.update_data(stage=stage)
        await state.set_state(PhotoForm.photos)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отправляйте фотографии. Подпись будет сохранена. После загрузки нажмите «Завершить».",
                reply_markup=kb([["✅ Завершить добавление"], ["❌ Отмена"]]),
            )

    @router.message(PhotoForm.photos, F.photo)
    async def v3_photo_receive(message: Message, state: FSMContext, db: Any, settings: Any, bot: Bot) -> None:
        uid = await actor_id(message, db)
        data = await state.get_data()
        oid = int(data["object_id"])
        photo = message.photo[-1]
        folder = settings.portfolio_dir / f"order_{oid}"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", photo.file_unique_id)
        target = folder / f"{datetime.now(settings.timezone):%Y%m%d_%H%M%S_%f}_{safe}.jpg"
        local_path: str | None = None
        try:
            await bot.download(photo, destination=target)
            local_path = str(target)
        except Exception:
            logging.exception("Не удалось сохранить локальную копию фото")
        pid = await db.execute(
            """INSERT INTO portfolio_photos(
                   order_id,stage,telegram_file_id,telegram_file_unique_id,local_path,
                   caption,created_by,created_at_utc
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (oid, data["stage"], photo.file_id, photo.file_unique_id, local_path, (message.caption or "")[:1000] or None, uid, now_iso()),
        )
        count = int(data.get("photo_count", 0)) + 1
        await state.update_data(photo_count=count)
        await message.answer(f"✅ Фото №{pid} сохранено. Загружено: {count}.")

    @router.message(PhotoForm.photos, F.text == "✅ Завершить добавление")
    async def v3_photo_finish(message: Message, state: FSMContext) -> None:
        count = int((await state.get_data()).get("photo_count", 0))
        await state.clear()
        await message.answer(f"✅ Сохранено фотографий: {count}.", reply_markup=portfolio_menu())

    @router.message(PhotoForm.photos)
    async def v3_photo_wrong(message: Message) -> None:
        await message.answer("Отправьте фотографию или нажмите «Завершить добавление».")

    async def send_portfolio_rows(target: Message, rows: Sequence[aiosqlite.Row], db: Any, settings: Any, viewer_id: int | None = None) -> None:
        if not rows:
            await target.answer("Фотографий не найдено.")
            return
        admin = await is_admin(viewer_id if viewer_id is not None else target.from_user.id, db, settings)
        for row in rows[:30]:
            tags: list[str] = [PHOTO_STAGES.get(row["stage"], row["stage"])]
            if int(row["is_best"]):
                tags.append("⭐ Лучшее")
            if int(row["for_site"]):
                tags.append("🌐 Для сайта")
            if row["before_after"] == "before":
                tags.append("⬅️ До")
            elif row["before_after"] == "after":
                tags.append("➡️ После")
            caption = (
                f"📦 <b>{html.escape(row['object_title'])}</b>\n"
                f"🏷 {html.escape(row['category'] or 'Другое')}\n"
                f"{' · '.join(tags)}"
            )
            if row["caption"]:
                caption += f"\n📝 {html.escape(row['caption'])}"
            markup = None
            if admin:
                markup = inline(
                    [
                        [("⭐ Лучшее", f"v3_pfbest:{row['id']}"), ("🌐 Для сайта", f"v3_pfsite:{row['id']}")],
                        [("⬅️ До", f"v3_pfbef:{row['id']}"), ("➡️ После", f"v3_pfaft:{row['id']}")],
                        [("🗑 Удалить", f"v3_pfdel:{row['id']}")],
                    ]
                )
            await target.answer_photo(row["telegram_file_id"], caption=caption, reply_markup=markup)

    async def portfolio_query(db: Any, where: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        return await db.fetchall(
            f"""SELECT p.*,o.title object_title,o.category
                FROM portfolio_photos p JOIN orders o ON o.id=p.order_id
                WHERE p.deleted=0 AND {where} ORDER BY p.id DESC LIMIT 50""",
            params,
        )

    @router.message(F.text == "📦 Фото по объектам")
    async def v3_portfolio_objects(message: Message, db: Any) -> None:
        rows = await db.fetchall(
            """SELECT o.id,o.title,COUNT(p.id) photo_count FROM orders o
               JOIN portfolio_photos p ON p.order_id=o.id AND p.deleted=0
               GROUP BY o.id,o.title ORDER BY o.id DESC LIMIT 50"""
        )
        if not rows:
            await message.answer("Фотографий объектов пока нет.")
            return
        await message.answer("Выберите объект:", reply_markup=objects_markup(rows, "v3_pobj"))

    @router.callback_query(F.data.startswith("v3_pobj:"))
    @router.callback_query(F.data.startswith("v3_pview:"))
    async def v3_portfolio_object_stage(callback: CallbackQuery) -> None:
        oid = int(callback.data.split(":", 1)[1])
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите этап:", reply_markup=stages_markup(f"v3_pof_{oid}", True))

    @router.callback_query(F.data.regexp(r"^v3_pof_\d+:(all|measurement|manufacturing|installation|finished)$"))
    async def v3_portfolio_object_send(callback: CallbackQuery, db: Any, settings: Any) -> None:
        prefix, stage = callback.data.split(":", 1)
        oid = int(prefix.rsplit("_", 1)[1])
        if stage == "all":
            rows = await portfolio_query(db, "p.order_id=?", (oid,))
        else:
            rows = await portfolio_query(db, "p.order_id=? AND p.stage=?", (oid, stage))
        await callback.answer()
        if callback.message:
            await send_portfolio_rows(callback.message, rows, db, settings, callback.from_user.id)

    @router.message(F.text == "🏷 Фото по категориям")
    async def v3_portfolio_categories(message: Message, db: Any) -> None:
        rows = await db.fetchall(
            """SELECT o.category,COUNT(p.id) photo_count FROM portfolio_photos p
               JOIN orders o ON o.id=p.order_id WHERE p.deleted=0
               GROUP BY o.category ORDER BY o.category"""
        )
        if not rows:
            await message.answer("Категорий с фотографиями пока нет.")
            return
        buttons = [[(f"{row['category']} — {row['photo_count']}", f"v3_pcat:{index}")]
                   for index, row in enumerate(rows)]
        await message.answer("Выберите категорию:", reply_markup=inline(buttons))

    @router.callback_query(F.data.startswith("v3_pcat:"))
    async def v3_portfolio_category_send(callback: CallbackQuery, db: Any, settings: Any) -> None:
        categories_rows = await db.fetchall(
            """SELECT o.category,COUNT(p.id) photo_count FROM portfolio_photos p
               JOIN orders o ON o.id=p.order_id WHERE p.deleted=0
               GROUP BY o.category ORDER BY o.category"""
        )
        categories = [str(row["category"]) for row in categories_rows]
        index = int(callback.data.split(":", 1)[1])
        if not 0 <= index < len(categories):
            await callback.answer("Список категорий устарел. Откройте его заново.", show_alert=True)
            return
        rows = await portfolio_query(db, "o.category=?", (categories[index],))
        await callback.answer()
        if callback.message:
            await send_portfolio_rows(callback.message, rows, db, settings, callback.from_user.id)

    @router.message(F.text == "🕓 Недавно добавленные")
    async def v3_portfolio_recent(message: Message, db: Any, settings: Any) -> None:
        await send_portfolio_rows(message, await portfolio_query(db, "1=1"), db, settings)

    @router.message(F.text == "⭐ Лучшие работы")
    async def v3_portfolio_best(message: Message, db: Any, settings: Any) -> None:
        await send_portfolio_rows(message, await portfolio_query(db, "p.is_best=1"), db, settings)

    @router.message(F.text == "🌐 Для сайта")
    async def v3_portfolio_site(message: Message, db: Any, settings: Any) -> None:
        await send_portfolio_rows(message, await portfolio_query(db, "p.for_site=1"), db, settings)

    @router.message(F.text == "↔️ До и после")
    async def v3_portfolio_before_after(message: Message, db: Any, settings: Any) -> None:
        await send_portfolio_rows(message, await portfolio_query(db, "p.before_after IN ('before','after')"), db, settings)

    @router.callback_query(F.data.startswith("v3_beforeafter:"))
    async def v3_object_before_after(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        rows = await portfolio_query(db, "p.order_id=? AND p.before_after IN ('before','after')", (oid,))
        await callback.answer()
        if callback.message:
            await send_portfolio_rows(callback.message, rows, db, settings, callback.from_user.id)

    async def photo_flag(callback: CallbackQuery, db: Any, settings: Any, field: str, value: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        pid = int(callback.data.split(":", 1)[1])
        if field in {"is_best", "for_site"}:
            row = await db.fetchone(f"SELECT {field} FROM portfolio_photos WHERE id=?", (pid,))
            value = 0 if row and int(row[field]) else 1
        await db.execute(f"UPDATE portfolio_photos SET {field}=? WHERE id=?", (value, pid))
        await callback.answer("Метка изменена")

    @router.callback_query(F.data.startswith("v3_pfbest:"))
    async def v3_photo_best(callback: CallbackQuery, db: Any, settings: Any) -> None:
        await photo_flag(callback, db, settings, "is_best", 1)

    @router.callback_query(F.data.startswith("v3_pfsite:"))
    async def v3_photo_site(callback: CallbackQuery, db: Any, settings: Any) -> None:
        await photo_flag(callback, db, settings, "for_site", 1)

    @router.callback_query(F.data.startswith("v3_pfbef:"))
    async def v3_photo_before(callback: CallbackQuery, db: Any, settings: Any) -> None:
        await photo_flag(callback, db, settings, "before_after", "before")

    @router.callback_query(F.data.startswith("v3_pfaft:"))
    async def v3_photo_after(callback: CallbackQuery, db: Any, settings: Any) -> None:
        await photo_flag(callback, db, settings, "before_after", "after")

    @router.callback_query(F.data.startswith("v3_pfdel:"))
    async def v3_photo_delete(callback: CallbackQuery, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        pid = int(callback.data.split(":", 1)[1])
        uid = await actor_id(callback, db)
        await db.execute(
            "UPDATE portfolio_photos SET deleted=1,deleted_by=?,deleted_at_utc=? WHERE id=?",
            (uid, now_iso(), pid),
        )
        await callback.answer("Фото удалено")
        if callback.message:
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                await callback.message.edit_reply_markup(reply_markup=None)

    # -----------------------------------------------------------------------
    # Финансы
    # -----------------------------------------------------------------------

    @router.message(F.text == "💰 Финансы")
    @router.message(Command("expenses"))
    async def v3_finance_menu(message: Message, db: Any, settings: Any) -> None:
        await actor_id(message, db)
        await message.answer("💰 <b>Финансы</b>", reply_markup=finance_menu(await is_admin(message.from_user.id, db, settings)))

    @router.message(F.text == "➕ Добавить расход")
    async def v3_expense_start(message: Message, state: FSMContext, db: Any) -> None:
        await actor_id(message, db)
        await state.clear()
        await state.update_data(origin="finance")
        await state.set_state(ExpenseFormV3.link_type)
        await message.answer(
            "К чему относится расход?",
            reply_markup=inline(
                [
                    [("📦 К объекту", "v3_exlink:object")],
                    [("📝 К делу мастерской", "v3_exlink:workshop")],
                    [("📌 Без привязки", "v3_exlink:none")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("v3_exp_o:"))
    async def v3_expense_from_object(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.update_data(link_type="object", object_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(ExpenseFormV3.category)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите категорию расхода:", reply_markup=inline([[(x, f"v3_exc:{i}")] for i, x in enumerate(EXPENSE_CATEGORIES)]))

    @router.callback_query(F.data.startswith("v3_exp_w:"))
    async def v3_expense_from_workshop(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.update_data(link_type="workshop", todo_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(ExpenseFormV3.category)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите категорию расхода:", reply_markup=inline([[(x, f"v3_exc:{i}")] for i, x in enumerate(EXPENSE_CATEGORIES)]))

    @router.callback_query(ExpenseFormV3.link_type, F.data.startswith("v3_exlink:"))
    async def v3_expense_link_type(callback: CallbackQuery, state: FSMContext, db: Any) -> None:
        link_type = callback.data.split(":", 1)[1]
        await state.update_data(link_type=link_type)
        await callback.answer()
        if link_type == "none":
            await state.set_state(ExpenseFormV3.category)
            if callback.message:
                await callback.message.answer("Выберите категорию:", reply_markup=inline([[(x, f"v3_exc:{i}")] for i, x in enumerate(EXPENSE_CATEGORIES)]))
            return
        await state.set_state(ExpenseFormV3.link_id)
        if not callback.message:
            return
        if link_type == "object":
            await callback.message.answer("Выберите объект:", reply_markup=objects_markup(await list_objects(db, "active"), "v3_exo"))
        else:
            rows = await db.fetchall("SELECT id,title FROM todos WHERE status<>'done' ORDER BY id DESC LIMIT 50")
            await callback.message.answer(
                "Выберите дело мастерской:",
                reply_markup=inline([[(f"№{r['id']} — {str(r['title'])[:38]}", f"v3_exw:{r['id']}")] for r in rows]),
            )

    @router.callback_query(ExpenseFormV3.link_id, F.data.startswith("v3_exo:"))
    async def v3_expense_object(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(object_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(ExpenseFormV3.category)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите категорию:", reply_markup=inline([[(x, f"v3_exc:{i}")] for i, x in enumerate(EXPENSE_CATEGORIES)]))

    @router.callback_query(ExpenseFormV3.link_id, F.data.startswith("v3_exw:"))
    async def v3_expense_workshop(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(todo_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(ExpenseFormV3.category)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Выберите категорию:", reply_markup=inline([[(x, f"v3_exc:{i}")] for i, x in enumerate(EXPENSE_CATEGORIES)]))

    @router.callback_query(ExpenseFormV3.category, F.data.startswith("v3_exc:"))
    async def v3_expense_category(callback: CallbackQuery, state: FSMContext) -> None:
        index = int(callback.data.split(":", 1)[1])
        await state.update_data(category=EXPENSE_CATEGORIES[index])
        await state.set_state(ExpenseFormV3.amount)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите сумму расхода:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(ExpenseFormV3.amount, F.text)
    async def v3_expense_amount(message: Message, state: FSMContext) -> None:
        try:
            value = parse_amount(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(amount=str(value))
        await state.set_state(ExpenseFormV3.description)
        await message.answer(
            "Что было куплено или оплачено? Можно пропустить.",
            reply_markup=kb([["Пропустить"], ["❌ Отмена"]]),
        )

    @router.message(ExpenseFormV3.description, F.text)
    async def v3_expense_description(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Пропустить" else (message.text or "").strip()
        await state.update_data(description=value[:1000])
        await state.set_state(ExpenseFormV3.expense_date)
        await message.answer("Выберите дату расхода или введите её:", reply_markup=kb([["Сегодня"], ["🗓 Ввести дату"], ["❌ Отмена"]]))

    @router.message(ExpenseFormV3.expense_date, F.text)
    async def v3_expense_date(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            selected = parse_date_choice(message.text or "", settings.timezone, allow_past=True)
            if selected is None:
                raise ValueError("Укажите дату")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(expense_date=selected.isoformat(), receipt_file_ids=[])
        await state.set_state(ExpenseFormV3.receipt)
        await message.answer(
            "🧾 Отправьте одно фото чека или альбом. После загрузки нажмите «✅ Готово».\n\n"
            "Фото чека необязательно.",
            reply_markup=kb([["✅ Готово", "Без чека"], ["❌ Отмена"]]),
        )

    async def save_v3_expense(message: Message, state: FSMContext, db: Any) -> None:
        uid = await actor_id(message, db)
        data = await state.get_data()
        receipts = list(dict.fromkeys(data.get("receipt_file_ids") or []))
        first_receipt = receipts[0] if receipts else None
        eid = await db.execute(
            """INSERT INTO expenses(
                   order_id,todo_id,amount,category,description,receipt_file_id,
                   created_by,expense_date,created_at_utc,deleted
               ) VALUES(?,?,?,?,?,?,?,?,?,0)""",
            (
                data.get("object_id"), data.get("todo_id"), data["amount"], data["category"],
                data["description"], first_receipt, uid, data["expense_date"], now_iso(),
            ),
        )
        for file_id in receipts:
            await db.execute(
                "INSERT OR IGNORE INTO expense_receipts(expense_id,telegram_file_id,created_at_utc) VALUES(?,?,?)",
                (eid, file_id, now_iso()),
            )
        await state.clear()
        receipt_text = f" Фото чека: {len(receipts)}." if receipts else " Без чека."
        await message.answer(f"✅ Расход №{eid} записан.{receipt_text}")

    @router.message(ExpenseFormV3.receipt, F.photo)
    async def v3_expense_receipt(message: Message, state: FSMContext) -> None:
        lock = RECEIPT_LOCKS.setdefault(message.from_user.id, asyncio.Lock())
        async with lock:
            data = await state.get_data()
            receipts = list(data.get("receipt_file_ids") or [])
            file_id = message.photo[-1].file_id
            if file_id not in receipts:
                receipts.append(file_id)
                await state.update_data(receipt_file_ids=receipts)
            count = len(receipts)
        if not message.media_group_id:
            await message.answer(
                f"✅ Фото чека добавлено: {count}. Можно отправить ещё или нажать «✅ Готово».",
                reply_markup=kb([["✅ Готово"], ["Без чека"], ["❌ Отмена"]]),
            )

    @router.message(ExpenseFormV3.receipt, F.text.in_({"✅ Готово", "Без чека", "Пропустить"}))
    async def v3_expense_finish_receipts(message: Message, state: FSMContext, db: Any) -> None:
        if message.text in {"Без чека", "Пропустить"}:
            await state.update_data(receipt_file_ids=[])
        else:
            await asyncio.sleep(0.8)
        await save_v3_expense(message, state, db)

    @router.message(ExpenseFormV3.receipt)
    async def v3_expense_receipt_wrong(message: Message) -> None:
        await message.answer("Отправьте фотографию чека, альбом или нажмите «✅ Готово» / «Без чека».")

    async def show_expenses(message: Message, db: Any, settings: Any, where: str = "1=1", params: Sequence[Any] = (), viewer_id: int | None = None) -> None:
        effective_user_id = viewer_id if viewer_id is not None else message.from_user.id
        admin = await is_admin(effective_user_id, db, settings)
        clause = where
        values = list(params)
        if not admin:
            clause += " AND u.telegram_id=?"
            values.append(effective_user_id)
        rows = await db.fetchall(
            f"""SELECT e.*,u.full_name creator_name,o.title object_title,t.title todo_title,
                       (SELECT COUNT(*) FROM expense_receipts er WHERE er.expense_id=e.id) receipt_count
                FROM expenses e JOIN users u ON u.id=e.created_by
                LEFT JOIN orders o ON o.id=e.order_id LEFT JOIN todos t ON t.id=e.todo_id
                WHERE e.deleted=0 AND {clause} ORDER BY e.id DESC LIMIT 40""",
            values,
        )
        if not rows:
            await message.answer("Расходов не найдено.")
            return
        for row in rows:
            link = row["object_title"] or row["todo_title"] or "Без привязки"
            receipt_count = int(row["receipt_count"] or 0)
            text = (
                f"💰 <b>Расход №{row['id']}</b>\n"
                f"<b>Сумма:</b> {money(row['amount'])}\n"
                f"<b>Категория:</b> {html.escape(row['category'])}\n"
                f"<b>Привязка:</b> {html.escape(link)}\n"
                f"<b>Описание:</b> {html.escape(row['description'] or '—')}\n"
                f"<b>Дата:</b> {row['expense_date']}\n"
                f"<b>Добавил:</b> {html.escape(row['creator_name'])}\n"
                f"<b>Фото чека:</b> {receipt_count if receipt_count else 'нет'}"
            )
            receipt_buttons: list[list[tuple[str, str]]] = []
            if receipt_count:
                receipt_buttons.append([("🧾 Посмотреть чек", f"v3_receipts:{row['id']}")])
            receipt_buttons.append([("📸 Добавить фото чека", f"v3_receipt_add:{row['id']}")])
            markup = inline(receipt_buttons)
            await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("v3_receipt_add:"))
    async def v3_add_expense_receipt_start(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        expense_id = int(callback.data.split(":", 1)[1])
        expense = await db.fetchone(
            """SELECT e.id,u.telegram_id creator_telegram_id
               FROM expenses e JOIN users u ON u.id=e.created_by
               WHERE e.id=? AND e.deleted=0""",
            (expense_id,),
        )
        if not expense:
            await callback.answer("Расход не найден", show_alert=True)
            return
        if not await is_admin(callback.from_user.id, db, settings) and int(expense["creator_telegram_id"]) != callback.from_user.id:
            await callback.answer("Добавлять чек может автор расхода или администратор", show_alert=True)
            return
        await state.clear()
        await state.update_data(expense_id=expense_id, receipt_file_ids=[])
        await state.set_state(ReceiptAddForm.photos)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"📸 Отправьте фото чека к расходу №{expense_id}. Можно отправить альбом.",
                reply_markup=kb([["✅ Готово"], ["❌ Отмена"]]),
            )

    @router.message(ReceiptAddForm.photos, F.photo)
    async def v3_add_expense_receipt_photo(message: Message, state: FSMContext) -> None:
        lock = RECEIPT_LOCKS.setdefault(message.from_user.id, asyncio.Lock())
        async with lock:
            data = await state.get_data()
            receipts = list(data.get("receipt_file_ids") or [])
            file_id = message.photo[-1].file_id
            if file_id not in receipts:
                receipts.append(file_id)
                await state.update_data(receipt_file_ids=receipts)
            count = len(receipts)
        if not message.media_group_id:
            await message.answer(f"✅ Добавлено фото: {count}. Отправьте ещё или нажмите «✅ Готово».")

    @router.message(ReceiptAddForm.photos, F.text == "✅ Готово")
    async def v3_add_expense_receipt_finish(message: Message, state: FSMContext, db: Any) -> None:
        await asyncio.sleep(0.8)
        data = await state.get_data()
        expense_id = int(data["expense_id"])
        receipts = list(dict.fromkeys(data.get("receipt_file_ids") or []))
        if not receipts:
            await message.answer("Сначала отправьте хотя бы одну фотографию чека.")
            return
        for file_id in receipts:
            await db.execute(
                "INSERT OR IGNORE INTO expense_receipts(expense_id,telegram_file_id,created_at_utc) VALUES(?,?,?)",
                (expense_id, file_id, now_iso()),
            )
        await db.execute(
            "UPDATE expenses SET receipt_file_id=COALESCE(receipt_file_id,?) WHERE id=?",
            (receipts[0], expense_id),
        )
        await state.clear()
        await message.answer(f"✅ К расходу №{expense_id} добавлено фото: {len(receipts)}.")

    @router.message(ReceiptAddForm.photos)
    async def v3_add_expense_receipt_wrong(message: Message) -> None:
        await message.answer("Отправьте фотографию, альбом или нажмите «✅ Готово».")

    @router.callback_query(F.data.startswith("v3_receipts:"))
    async def v3_view_expense_receipts(callback: CallbackQuery, db: Any, settings: Any) -> None:
        expense_id = int(callback.data.split(":", 1)[1])
        expense = await db.fetchone(
            """SELECT e.id,u.telegram_id creator_telegram_id
               FROM expenses e JOIN users u ON u.id=e.created_by
               WHERE e.id=? AND e.deleted=0""",
            (expense_id,),
        )
        if not expense:
            await callback.answer("Расход не найден", show_alert=True)
            return
        if not await is_admin(callback.from_user.id, db, settings) and int(expense["creator_telegram_id"]) != callback.from_user.id:
            await callback.answer("Этот чек доступен автору расхода и администраторам", show_alert=True)
            return
        receipts = await db.fetchall(
            "SELECT telegram_file_id FROM expense_receipts WHERE expense_id=? ORDER BY id",
            (expense_id,),
        )
        await callback.answer()
        if not callback.message:
            return
        if not receipts:
            await callback.message.answer("У этого расхода нет фотографий чека.")
            return
        for index, receipt in enumerate(receipts, start=1):
            await callback.message.answer_photo(
                receipt["telegram_file_id"],
                caption=f"🧾 Чек к расходу №{expense_id} · фото {index} из {len(receipts)}",
            )

    @router.message(F.text == "📋 Расходы")
    async def v3_expenses_list(message: Message, db: Any, settings: Any) -> None:
        await show_expenses(message, db, settings)

    @router.message(F.text == "🏭 Расходы мастерской")
    async def v3_workshop_expenses(message: Message, db: Any, settings: Any) -> None:
        await show_expenses(message, db, settings, "e.todo_id IS NOT NULL OR (e.todo_id IS NULL AND e.order_id IS NULL)")

    @router.callback_query(F.data.startswith("v3_wexpenses:"))
    async def v3_workshop_expenses_card(callback: CallbackQuery, db: Any, settings: Any) -> None:
        wid = int(callback.data.split(":", 1)[1])
        await callback.answer()
        if callback.message:
            await show_expenses(callback.message, db, settings, "e.todo_id=?", (wid,), callback.from_user.id)

    @router.message(F.text == "📦 Финансы по объектам")
    async def v3_finance_objects(message: Message, db: Any) -> None:
        await message.answer("Выберите объект:", reply_markup=objects_markup(await list_objects(db, "all"), "v3_ofin"))

    @router.callback_query(F.data.startswith("v3_ofin:"))
    async def v3_object_finance(callback: CallbackQuery, db: Any, settings: Any) -> None:
        oid = int(callback.data.split(":", 1)[1])
        expense_row = await db.fetchone(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) total FROM expenses WHERE deleted=0 AND order_id=?", (oid,)
        )
        admin = await is_admin(callback.from_user.id, db, settings)
        lines = [f"💰 <b>Финансы объекта №{oid}</b>", f"Расходы: <b>{money(expense_row['total'] or 0)}</b>"]
        if admin:
            income_row = await db.fetchone(
                "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) total FROM incomes WHERE deleted=0 AND order_id=?", (oid,)
            )
            income = Decimal(str(income_row["total"] or 0))
            expense = Decimal(str(expense_row["total"] or 0))
            lines += [f"Доходы: <b>{money(income)}</b>", f"Разница: <b>{money(income-expense)}</b>"]
        await callback.answer()
        if callback.message:
            await callback.message.answer("\n".join(lines))

    async def send_finance_export(
        message: Message,
        db: Any,
        settings: Any,
        start: date,
        end: date,
    ) -> None:
        if start > end:
            start, end = end, start

        expenses = await db.fetchall(
            """SELECT e.id,e.expense_date operation_date,e.amount,e.category operation_kind,
                      e.description,e.receipt_file_id,o.title object_title,t.title todo_title,
                      u.full_name creator_name,
                      (SELECT COUNT(*) FROM expense_receipts er WHERE er.expense_id=e.id) receipt_count
               FROM expenses e
               JOIN users u ON u.id=e.created_by
               LEFT JOIN orders o ON o.id=e.order_id
               LEFT JOIN todos t ON t.id=e.todo_id
               WHERE e.deleted=0 AND e.expense_date BETWEEN ? AND ?""",
            (start.isoformat(), end.isoformat()),
        )
        incomes = await db.fetchall(
            """SELECT i.id,i.income_date operation_date,i.amount,i.payment_type operation_kind,
                      i.description,o.title object_title,u.full_name creator_name
               FROM incomes i
               JOIN users u ON u.id=i.created_by
               LEFT JOIN orders o ON o.id=i.order_id
               WHERE i.deleted=0 AND i.income_date BETWEEN ? AND ?""",
            (start.isoformat(), end.isoformat()),
        )

        if not expenses and not incomes:
            await message.answer(
                f"За период {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')} финансовых операций нет."
            )
            return

        total_expenses = sum((Decimal(str(row["amount"])) for row in expenses), Decimal("0"))
        total_incomes = sum((Decimal(str(row["amount"])) for row in incomes), Decimal("0"))
        balance = total_incomes - total_expenses

        operations: list[dict[str, Any]] = []
        for row in expenses:
            operations.append(
                {
                    "type": "Расход",
                    "id": row["id"],
                    "date": row["operation_date"],
                    "object": row["object_title"] or "",
                    "workshop": row["todo_title"] or "",
                    "kind": row["operation_kind"],
                    "description": row["description"] or "",
                    "amount": Decimal(str(row["amount"])),
                    "creator": row["creator_name"],
                    "receipt": "Да" if int(row["receipt_count"] or 0) else "Нет",
                    "receipt_count": int(row["receipt_count"] or 0),
                }
            )
        for row in incomes:
            operations.append(
                {
                    "type": "Доход",
                    "id": row["id"],
                    "date": row["operation_date"],
                    "object": row["object_title"] or "",
                    "workshop": "",
                    "kind": row["operation_kind"],
                    "description": row["description"] or "",
                    "amount": Decimal(str(row["amount"])),
                    "creator": row["creator_name"],
                    "receipt": "",
                    "receipt_count": "",
                }
            )
        operations.sort(key=lambda item: (item["date"], item["type"], item["id"]))

        def csv_amount(value: Decimal) -> str:
            return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",")

        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Финансовая выгрузка"])
        writer.writerow(["Период", start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")])
        writer.writerow(["Доходы", csv_amount(total_incomes)])
        writer.writerow(["Расходы", csv_amount(total_expenses)])
        writer.writerow(["Разница", csv_amount(balance)])
        writer.writerow([])
        writer.writerow(
            [
                "Операция",
                "ID",
                "Дата",
                "Объект",
                "Дело мастерской",
                "Категория / тип платежа",
                "Описание",
                "Сумма, ₽",
                "Добавил",
                "Чек",
                "Количество фото чека",
            ]
        )
        for item in operations:
            writer.writerow(
                [
                    item["type"],
                    item["id"],
                    item["date"],
                    item["object"],
                    item["workshop"],
                    item["kind"],
                    item["description"],
                    csv_amount(item["amount"]),
                    item["creator"],
                    item["receipt"],
                    item["receipt_count"],
                ]
            )

        content = ("\ufeff" + output.getvalue()).encode("utf-8")
        filename = f"finances_{start.isoformat()}_{end.isoformat()}.csv"
        await message.answer_document(
            BufferedInputFile(content, filename=filename),
            caption=(
                f"📤 <b>Финансовая выгрузка</b>\n"
                f"Период: {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}\n"
                f"Доходы: <b>{money(total_incomes)}</b>\n"
                f"Расходы: <b>{money(total_expenses)}</b>\n"
                f"Разница: <b>{money(balance)}</b>"
            ),
        )

    @router.message(F.text == "📤 Выгрузить финансы")
    async def v3_finance_export_menu(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await message.answer("Только администратор может выгружать финансы.")
            return
        await state.clear()
        await message.answer(
            "За какой период выгрузить доходы и расходы?",
            reply_markup=inline(
                [
                    [("Сегодня", "v3_fexp:today"), ("Текущий месяц", "v3_fexp:month")],
                    [("Всё время", "v3_fexp:all")],
                    [("🗓 Выбрать период", "v3_fexp:custom")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("v3_fexp:"))
    async def v3_finance_export_period(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        choice = callback.data.split(":", 1)[1]
        today = datetime.now(settings.timezone).date()
        await callback.answer()
        if choice == "custom":
            await state.clear()
            await state.set_state(FinanceExportForm.start_date)
            if callback.message:
                await callback.message.answer(
                    "Введите начальную дату, например <code>01.07.2026</code>:",
                    reply_markup=kb([["Сегодня"], ["❌ Отмена"]]),
                )
            return
        if choice == "today":
            start = end = today
        elif choice == "month":
            start, end = today.replace(day=1), today
        else:
            earliest = await db.fetchone(
                """SELECT MIN(operation_date) min_date FROM (
                       SELECT expense_date operation_date FROM expenses WHERE deleted=0
                       UNION ALL
                       SELECT income_date operation_date FROM incomes WHERE deleted=0
                   )"""
            )
            if not earliest or not earliest["min_date"]:
                if callback.message:
                    await callback.message.answer("Финансовых операций пока нет.")
                return
            start, end = date.fromisoformat(earliest["min_date"]), today
        if callback.message:
            await send_finance_export(callback.message, db, settings, start, end)

    @router.message(FinanceExportForm.start_date, F.text)
    async def v3_finance_export_start(message: Message, state: FSMContext, settings: Any) -> None:
        try:
            selected = parse_date_choice(message.text or "", settings.timezone, allow_past=True)
            if selected is None:
                raise ValueError("Укажите дату")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(start_date=selected.isoformat())
        await state.set_state(FinanceExportForm.end_date)
        await message.answer(
            "Введите конечную дату:",
            reply_markup=kb([["Сегодня"], ["❌ Отмена"]]),
        )

    @router.message(FinanceExportForm.end_date, F.text)
    async def v3_finance_export_end(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await state.clear()
            await message.answer("Только администратор.")
            return
        try:
            selected = parse_date_choice(message.text or "", settings.timezone, allow_past=True)
            if selected is None:
                raise ValueError("Укажите дату")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        start = date.fromisoformat(data["start_date"])
        await state.clear()
        await send_finance_export(message, db, settings, start, selected)

    # Доходы — только администратор
    @router.message(F.text == "💵 Добавить доход")
    @router.message(Command("income"))
    async def v3_income_start(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await message.answer("Только администратор может добавлять доходы.")
            return
        await state.clear()
        await state.set_state(IncomeFormV3.object_id)
        await message.answer("Выберите объект:", reply_markup=objects_markup(await list_objects(db, "all"), "v3_income_o"))

    @router.callback_query(F.data.startswith("v3_inc_o:"))
    async def v3_income_from_object(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        await state.clear()
        await state.update_data(object_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(IncomeFormV3.amount)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите сумму дохода:", reply_markup=kb([["❌ Отмена"]]))

    @router.callback_query(IncomeFormV3.object_id, F.data.startswith("v3_income_o:"))
    async def v3_income_object(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(object_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(IncomeFormV3.amount)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите сумму дохода:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(IncomeFormV3.amount, F.text)
    async def v3_income_amount(message: Message, state: FSMContext) -> None:
        try:
            value = parse_amount(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(amount=str(value))
        await state.set_state(IncomeFormV3.payment_type)
        await message.answer("Выберите тип платежа:", reply_markup=inline([[(x, f"v3_income_t:{i}")] for i, x in enumerate(INCOME_TYPES)]))

    @router.callback_query(IncomeFormV3.payment_type, F.data.startswith("v3_income_t:"))
    async def v3_income_type(callback: CallbackQuery, state: FSMContext) -> None:
        index = int(callback.data.split(":", 1)[1])
        await state.update_data(payment_type=INCOME_TYPES[index])
        await state.set_state(IncomeFormV3.description)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Добавьте описание или нажмите «Пропустить»:", reply_markup=kb([["Пропустить"], ["❌ Отмена"]]))

    @router.message(IncomeFormV3.description, F.text)
    async def v3_income_description(message: Message, state: FSMContext) -> None:
        value = "" if message.text == "Пропустить" else (message.text or "").strip()
        await state.update_data(description=value[:1000])
        await state.set_state(IncomeFormV3.income_date)
        await message.answer("Выберите дату получения:", reply_markup=kb([["Сегодня"], ["🗓 Ввести дату"], ["❌ Отмена"]]))

    @router.message(IncomeFormV3.income_date, F.text)
    async def v3_income_save(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await state.clear()
            await message.answer("Только администратор.")
            return
        try:
            selected = parse_date_choice(message.text or "", settings.timezone, allow_past=True)
            if selected is None:
                raise ValueError("Укажите дату")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        uid = await actor_id(message, db)
        data = await state.get_data()
        iid = await db.execute(
            """INSERT INTO incomes(order_id,amount,payment_type,description,created_by,income_date,created_at_utc,deleted)
               VALUES(?,?,?,?,?,?,?,0)""",
            (int(data["object_id"]), data["amount"], data["payment_type"], data.get("description") or None, uid, selected.isoformat(), now_iso()),
        )
        await state.clear()
        await message.answer(f"✅ Доход №{iid} записан.", reply_markup=finance_menu(True))

    @router.message(F.text == "📋 Доходы")
    async def v3_incomes_list(message: Message, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await message.answer("Только администратор.")
            return
        rows = await db.fetchall(
            """SELECT i.*,o.title object_title,u.full_name creator_name FROM incomes i
               LEFT JOIN orders o ON o.id=i.order_id JOIN users u ON u.id=i.created_by
               WHERE i.deleted=0 ORDER BY i.id DESC LIMIT 40"""
        )
        if not rows:
            await message.answer("Доходов пока нет.")
            return
        for row in rows:
            await message.answer(
                f"💵 <b>Доход №{row['id']}</b>\n"
                f"<b>Объект:</b> {html.escape(row['object_title'] or '—')}\n"
                f"<b>Сумма:</b> {money(row['amount'])}\n"
                f"<b>Тип:</b> {html.escape(row['payment_type'])}\n"
                f"<b>Дата:</b> {row['income_date']}"
            )

    # -----------------------------------------------------------------------
    # Изменение основных данных объекта
    # -----------------------------------------------------------------------

    @router.callback_query(F.data.startswith("v3_oedit:"))
    async def v3_object_edit_menu(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        oid = int(callback.data.split(":", 1)[1])
        await state.update_data(object_id=oid)
        await state.set_state(ObjectEditForm.field)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Что изменить?",
                reply_markup=inline(
                    [
                        [("Категория", "v3_oe:category"), ("Название", "v3_oe:title")],
                        [("Адрес", "v3_oe:address"), ("Описание", "v3_oe:description")],
                        [("Имя заказчика", "v3_oe:client")],
                        [("Телефон", "v3_oe:client_phone"), ("Telegram", "v3_oe:client_telegram")],
                    ]
                ),
            )

    @router.callback_query(ObjectEditForm.field, F.data.startswith("v3_oe:"))
    async def v3_object_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
        field = callback.data.split(":", 1)[1]
        await state.update_data(field=field)
        await state.set_state(ObjectEditForm.value)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите новое значение. Для очистки отправьте «-»:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(ObjectEditForm.value, F.text)
    async def v3_object_edit_save(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await state.clear()
            await message.answer("Только администратор.")
            return
        data = await state.get_data()
        field = data["field"]
        if field not in {"category", "title", "address", "description", "client", "client_phone", "client_telegram"}:
            await state.clear()
            await message.answer("Поле не поддерживается.")
            return
        value = None if (message.text or "").strip() == "-" else (message.text or "").strip()[:1500]
        await db.execute(f"UPDATE orders SET {field}=?,updated_at_utc=? WHERE id=?", (value, now_iso(), int(data["object_id"])))
        oid = int(data["object_id"])
        await state.clear()
        row = await fetch_object(db, oid)
        await message.answer("✅ Данные изменены.")
        if row:
            await message.answer(object_card(row, settings.timezone), reply_markup=object_actions(row, True))

    # -----------------------------------------------------------------------
    # Напоминания: завершённые и повторяющиеся списки
    # -----------------------------------------------------------------------

    @router.message(F.text == "✅ Выполненные напоминания")
    async def v3_done_reminders(message: Message, db: Any, settings: Any) -> None:
        uid = await actor_id(message, db)
        rows = await db.fetchall(
            """SELECT * FROM reminders WHERE creator_id=? AND sent_at_utc IS NOT NULL
               ORDER BY sent_at_utc DESC LIMIT 30""", (uid,)
        )
        if not rows:
            await message.answer("Выполненных напоминаний пока нет.")
            return
        for row in rows:
            await message.answer(
                f"✅ {html.escape(row['text'])}\n<i>{local_dt(row['sent_at_utc'], settings.timezone)}</i>"
            )

    @router.message(F.text == "🔁 Повторяющиеся напоминания")
    async def v3_repeat_reminders(message: Message, db: Any, settings: Any) -> None:
        uid = await actor_id(message, db)
        rows = await db.fetchall(
            """SELECT * FROM reminders WHERE creator_id=? AND repeat_rule<>'none' AND cancelled=0
               ORDER BY remind_at_utc""", (uid,)
        )
        if not rows:
            await message.answer("Повторяющихся напоминаний пока нет. Регулярные рабочие дела создаются в разделе «Дела мастерской».")
            return
        for row in rows:
            await message.answer(f"🔁 {html.escape(row['text'])}\n{local_dt(row['remind_at_utc'], settings.timezone)}")

    # -----------------------------------------------------------------------
    # Сотрудники
    # -----------------------------------------------------------------------

    @router.message(F.text == "👥 Сотрудники")
    @router.message(Command("staff"))
    async def v3_staff_menu(message: Message, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await message.answer("Только администратор.")
            return
        await message.answer("👥 <b>Сотрудники</b>", reply_markup=staff_menu())

    @router.message(F.text == "➕ Добавить сотрудника")
    async def v3_staff_add_start(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await message.answer("Только администратор.")
            return
        await state.clear()
        await state.set_state(StaffAddForm.telegram_id)
        await message.answer("Введите числовой Telegram ID сотрудника:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(StaffAddForm.telegram_id, F.text)
    async def v3_staff_add_id(message: Message, state: FSMContext, db: Any) -> None:
        raw = (message.text or "").strip()
        if not raw.isdigit() or len(raw) < 5:
            await message.answer("Telegram ID должен состоять только из цифр:")
            return
        telegram_id = int(raw)
        existing = await db.user_by_telegram_id(telegram_id)
        role = existing["role"] if existing and existing["role"] == "admin" else "employee"
        await state.update_data(telegram_id=telegram_id, role=role)
        await state.set_state(StaffAddForm.confirm)
        status = "восстановить доступ" if existing and not int(existing["active"]) else "добавить сотрудника"
        await message.answer(
            f"Подтвердите: {status} с ID <code>{telegram_id}</code>?",
            reply_markup=inline([[('✅ Подтвердить', 'v3_staff_add_ok'), ('❌ Отмена', 'v3_cancel')]]),
        )

    @router.callback_query(StaffAddForm.confirm, F.data == "v3_staff_add_ok")
    async def v3_staff_add_confirm(callback: CallbackQuery, state: FSMContext, db: Any, settings: Any, bot: Bot) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        data = await state.get_data()
        telegram_id = int(data["telegram_id"])
        await db.add_staff(telegram_id, data.get("role", "employee"))
        await state.clear()
        notified = await safe_notify(
            bot, telegram_id,
            "✅ Вам открыт доступ к рабочему боту «Фабрики Деталей». Нажмите /start и укажите имя командой /register Имя.",
        )
        await callback.answer("Доступ открыт")
        if callback.message:
            note = "Пользователь уведомлён." if notified else "Пользователь ещё не запускал бота; уведомить его не удалось."
            await callback.message.answer(f"✅ Доступ для <code>{telegram_id}</code> открыт. {note}", reply_markup=staff_menu())

    async def staff_list_markup(db: Any, active: bool, prefix: str) -> InlineKeyboardMarkup:
        rows = await db.staff(active)
        return inline(
            [[(f"{'🛡' if r['role']=='admin' else '👤'} {r['full_name']} · {r['telegram_id']}", f"{prefix}:{r['telegram_id']}")]
             for r in rows]
            or [[("Список пуст", "noop")]]
        )

    @router.message(F.text == "📋 Активные сотрудники")
    async def v3_staff_active(message: Message, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            return
        rows = await db.staff(True)
        if not rows:
            await message.answer("Активных сотрудников нет.")
            return
        for row in rows:
            role = "Администратор" if row["role"] == "admin" else "Сотрудник"
            markup = inline([[('🚫 Заблокировать', f"v3_staff_block:{row['telegram_id']}")]]) if int(row["telegram_id"]) not in settings.admin_ids else None
            await message.answer(
                f"👤 <b>{html.escape(row['full_name'])}</b>\nID: <code>{row['telegram_id']}</code>\nРоль: {role}",
                reply_markup=markup,
            )

    @router.message(F.text.in_({"🚫 Заблокированные сотрудники", "✅ Восстановить доступ"}))
    async def v3_staff_blocked(message: Message, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            return
        rows = await db.staff(False)
        if not rows:
            await message.answer("Заблокированных сотрудников нет.")
            return
        await message.answer("Выберите сотрудника для восстановления:", reply_markup=await staff_list_markup(db, False, "v3_staff_restore"))

    @router.callback_query(F.data.startswith("v3_staff_block:"))
    async def v3_staff_block(callback: CallbackQuery, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        telegram_id = int(callback.data.split(":", 1)[1])
        if telegram_id in settings.admin_ids or telegram_id == callback.from_user.id:
            await callback.answer("Нельзя заблокировать постоянного администратора", show_alert=True)
            return
        await db.set_staff_active(telegram_id, False)
        await callback.answer("Сотрудник заблокирован")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("v3_staff_restore:"))
    async def v3_staff_restore(callback: CallbackQuery, db: Any, settings: Any, bot: Bot) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        telegram_id = int(callback.data.split(":", 1)[1])
        row = await db.user_by_telegram_id(telegram_id)
        role = row["role"] if row and row["role"] == "admin" else "employee"
        await db.add_staff(telegram_id, role)
        await safe_notify(bot, telegram_id, "✅ Ваш доступ к рабочему боту восстановлен.")
        await callback.answer("Доступ восстановлен")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(F.text == "🛡 Назначить администратора")
    async def v3_staff_admin_choose(message: Message, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            return
        rows = [r for r in await db.staff(True) if r["role"] != "admin"]
        await message.answer(
            "Выберите сотрудника:",
            reply_markup=inline([[(f"{r['full_name']} · {r['telegram_id']}", f"v3_staff_admin:{r['telegram_id']}")] for r in rows] or [[("Нет кандидатов", "noop")]]),
        )

    @router.callback_query(F.data.startswith("v3_staff_admin:"))
    async def v3_staff_admin_set(callback: CallbackQuery, db: Any, settings: Any) -> None:
        if not await is_admin(callback.from_user.id, db, settings):
            await callback.answer("Только администратор", show_alert=True)
            return
        telegram_id = int(callback.data.split(":", 1)[1])
        await db.add_staff(telegram_id, "admin")
        await callback.answer("Администратор назначен")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(F.text == "✏️ Изменить имя")
    async def v3_staff_rename_choose(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            return
        await state.set_state(StaffRenameForm.employee)
        await message.answer("Выберите сотрудника:", reply_markup=await staff_list_markup(db, True, "v3_staff_rename"))

    @router.callback_query(StaffRenameForm.employee, F.data.startswith("v3_staff_rename:"))
    async def v3_staff_rename_selected(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(telegram_id=int(callback.data.split(":", 1)[1]))
        await state.set_state(StaffRenameForm.name)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Введите новое рабочее имя:", reply_markup=kb([["❌ Отмена"]]))

    @router.message(StaffRenameForm.name, F.text)
    async def v3_staff_rename_save(message: Message, state: FSMContext, db: Any, settings: Any) -> None:
        if not await is_admin(message.from_user.id, db, settings):
            await state.clear()
            return
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Имя слишком короткое:")
            return
        data = await state.get_data()
        await db.rename_user(int(data["telegram_id"]), name[:100])
        await state.clear()
        await message.answer("✅ Рабочее имя изменено.", reply_markup=staff_menu())

    # -----------------------------------------------------------------------
    # Общие callback-заглушки
    # -----------------------------------------------------------------------

    @router.callback_query(F.data == "noop")
    async def v3_noop(callback: CallbackQuery) -> None:
        await callback.answer()
