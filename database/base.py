import os
import aiosqlite

DB_DIR = "db"
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)


class BaseDatabase:
    """Базовый класс для всех баз данных проекта."""

    def __init__(self, db_name: str) -> None:
        self.db_name = db_name

    async def init_db(self) -> None:
        raise NotImplementedError

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_name)
