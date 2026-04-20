import os
from typing import Optional, Dict, Any

import aiosqlite

from database.base import BaseDatabase, DB_DIR


class CoinsDatabase(BaseDatabase):
    """База данных для управления списком активных монет."""

    def __init__(self, db_name: str = os.path.join(DB_DIR, "coins.db")) -> None:
        super().__init__(db_name)

    async def init_db(self) -> None:
        async with self._connect() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS coins (coin TEXT PRIMARY KEY, alias TEXT, is_active INTEGER)"
            )
            await db.commit()

    async def add_coin(self, coin: str, alias: str = "", is_active: int = 1) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO coins (coin, alias, is_active) VALUES (?, ?, ?)",
                (coin, alias, is_active)
            )
            await db.commit()

    async def get_coin(self, coin: str) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT coin, alias, is_active FROM coins WHERE coin=?", (coin,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {"coin": row['coin'], "alias": row['alias'], "is_active": bool(row['is_active'])}
