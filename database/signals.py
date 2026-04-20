import os
import aiosqlite
from typing import Dict, Any

from logger import bot_logger
from database.base import BaseDatabase, DB_DIR


class Database(BaseDatabase):
    """База данных для хранения входящих сигналов."""

    def __init__(self, db_name: str = os.path.join(DB_DIR, "signals.db")) -> None:
        super().__init__(db_name)

    async def init_db(self) -> None:
        async with self._connect() as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT,
                    signal_type TEXT,
                    direction TEXT,
                    price REAL,
                    target_price REAL,
                    received_at TEXT,
                    raw_text TEXT
                )
            ''')
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_received_at ON signals (received_at)"
            )
            await db.commit()
        bot_logger.info("База данных сигналов (signals.db) успешно инициализирована.")

    async def save_signal(self, data: Dict[str, Any]) -> bool:
        bot_logger.debug(f"Попытка записи сигнала в БД: {data.get('coin')} | {data.get('signal_type')}")
        try:
            async with self._connect() as db:
                await db.execute(
                    """INSERT INTO signals
                    (coin, signal_type, direction, price, target_price, received_at, raw_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        data['coin'],
                        data['signal_type'],
                        data.get('direction', 'LONG'),
                        data['price'],
                        data['target_price'],
                        data['received_at'],
                        data['raw_text']
                    )
                )
                await db.commit()
            bot_logger.info(f"Сигнал {data['coin']} успешно сохранен в базу.")
            return True
        except Exception as e:
            bot_logger.error(f"Критическая ошибка БД при сохранении сигнала {data.get('coin')}: {e}")
            return False
