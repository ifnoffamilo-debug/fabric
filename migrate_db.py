from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from bot import Database


async def migrate() -> None:
    load_dotenv()
    db_path = Path(os.getenv("DB_PATH", "data/factory_bot.sqlite3")).expanduser()
    db = Database(db_path)
    await db.init()
    print(f"OK: database updated: {db_path}")


if __name__ == "__main__":
    asyncio.run(migrate())
