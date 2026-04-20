import os
from typing import Optional, Any

import aiosqlite

from logger import bot_logger
from database.base import BaseDatabase, DB_DIR


class SettingsDatabase(BaseDatabase):
    """База данных для хранения настроек бота (DCA, TP)."""

    def __init__(self, db_name: str = os.path.join(DB_DIR, "settings.db")) -> None:
        super().__init__(db_name)

    async def init_db(self) -> None:
        async with self._connect() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
            )
            await db.commit()
        await self.ensure_defaults()
        bot_logger.info("База данных настроек (settings.db) успешно инициализирована.")

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cursor.fetchone()
            return row['value'] if row and row['value'] is not None else default

    async def set(self, key: str, value: Any) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
            )
            await db.commit()
        bot_logger.info(f"Параметр '{key}' был обновлен в БД. Новое значение: {value}")

    async def ensure_defaults(self) -> None:
        """Устанавливает значения по умолчанию, если они отсутствуют."""
        defaults = {
            "dca_0": "2", "dca_1": "4", "dca_2": "8", "dca_3": "16",
            "dca_level_1": "3.5", "dca_level_2": "6.5", "dca_level_3": "14.5",
            "tp_target": "1.5",
            "max_active_trades": "3",
        }
        async with self._connect() as db:
            for key, default_value in defaults.items():
                cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
                if await cursor.fetchone() is None:
                    await db.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)", (key, default_value)
                    )
                    bot_logger.info(f"БД Settings: значение по умолчанию '{key}': {default_value}")
            await db.commit()
