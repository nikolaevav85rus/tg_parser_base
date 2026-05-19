import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from logger import bot_logger
from database.base import BaseDatabase, DB_DIR


class TradesDatabase(BaseDatabase):
    """База данных для управления торговыми сделками (позициями)."""

    def __init__(self, db_name: str = os.path.join(DB_DIR, "trades.db")) -> None:
        super().__init__(db_name)

    async def init_db(self) -> None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT, step INTEGER, buy_p REAL, buy_a REAL,
                    dca1_p REAL, dca1_a REAL, dca2_p REAL, dca2_a REAL, dca3_p REAL, dca3_a REAL,
                    avg_p REAL, total_inv REAL, target_p REAL, created_at TEXT,
                    exit_p REAL, pnl REAL, pnl_p REAL, closed_at TEXT
                )
            ''')
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_coin_status ON trades (coin, status)"
            )
            await db.commit()
        await self._upgrade_db()

    async def _upgrade_db(self) -> None:
        """Миграции схемы БД. Каждая версия применяется ровно один раз."""
        migrations = {
            1: [
                ("leverage",     "INTEGER DEFAULT 1"),
                ("open_fee",     "REAL DEFAULT 0.0"),
                ("funding_fee",  "REAL DEFAULT 0.0"),
                ("close_fee",    "REAL DEFAULT 0.0"),
                ("net_pnl",      "REAL DEFAULT 0.0"),
                ("tp_order_id",  "TEXT"),
                ("dca_order_id", "TEXT"),
                ("status",       "TEXT DEFAULT 'TRADING'"),
            ],
            2: [
                ("closed_at",    "TEXT"),
            ]
        }

        async with self._connect() as db:
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            current_version = row[0] or 0

            for version in sorted(v for v in migrations if v > current_version):
                for col_name, col_def in migrations[version]:
                    try:
                        await db.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                        bot_logger.info(f"БД миграция v{version}: добавлена колонка {col_name}")
                    except Exception:
                        pass  # колонка уже существует

                if version >= 2:
                    await db.execute(
                        "UPDATE trades SET closed_at = created_at "
                        "WHERE status = 'closed' AND (closed_at IS NULL OR closed_at = '')"
                    )

                await db.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (version,)
                )
                bot_logger.info(f"БД: применена миграция v{version}")

            await db.commit()

    async def create_trade(
        self, coin: str, entry_p: float, inv: float, target_p: float,
        leverage: int = 1, open_fee: float = 0.0
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                '''INSERT INTO trades
                (coin, step, buy_p, buy_a, avg_p, total_inv, status, target_p, created_at, leverage, open_fee)
                VALUES (?, 0, ?, ?, ?, ?, 'TRADING', ?, ?, ?, ?)''',
                (coin, entry_p, inv, entry_p, inv, target_p,
                 datetime.now(timezone.utc).isoformat(), leverage, open_fee)
            )
            await db.commit()

    async def set_tp_order_id(self, coin: str, order_id: Optional[str]) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE trades SET tp_order_id=? WHERE coin=? AND status='TRADING'",
                (order_id, coin)
            )
            await db.commit()

    async def set_dca_order_id(self, coin: str, order_id: Optional[str]) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE trades SET dca_order_id=? WHERE coin=? AND status='TRADING'",
                (order_id, coin)
            )
            await db.commit()

    async def get_trading_trade(self, coin: str) -> Optional[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE coin=? AND status='TRADING' LIMIT 1", (coin,)
            )
            return await cursor.fetchone()

    async def update_trade_dca(
        self, coin: str, step: int, price: float, inv: float, fee: float = 0.0
    ) -> None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'",
                (coin,)
            )
            row = await cursor.fetchone()
            if not row:
                return

            old_inv, old_avg, old_fee = row['total_inv'], row['avg_p'], row['open_fee']
            new_inv = old_inv + inv
            new_avg = ((old_inv + inv) / ((old_inv / old_avg) + (inv / price))) if old_avg > 0 else price
            new_fee = old_fee + fee

            _valid_dca_cols = {1: ("dca1_p", "dca1_a"), 2: ("dca2_p", "dca2_a"), 3: ("dca3_p", "dca3_a")}
            if step not in _valid_dca_cols:
                bot_logger.error(f"БД: Недопустимый шаг DCA: {step}")
                return
            col_p, col_a = _valid_dca_cols[step]

            await db.execute(
                f"UPDATE trades SET step=?, {col_p}=?, {col_a}=?, avg_p=?, total_inv=?, open_fee=?"
                " WHERE coin=? AND status='TRADING'",
                (step, price, inv, new_avg, new_inv, new_fee, coin)
            )
            await db.commit()

    async def close_trade(
        self, coin: str, exit_price: float, close_fee: float = 0.0, funding_fee: float = 0.0
    ) -> Tuple[float, float, float]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'",
                (coin,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0.0, 0.0, 0.0

            inv, avg, open_fee = row['total_inv'], row['avg_p'], row['open_fee']
            qty = inv / avg if avg > 0 else 0
            gross_pnl = (qty * exit_price) - inv
            gross_pnl_p = (gross_pnl / inv * 100) if inv > 0 else 0
            net_pnl = gross_pnl - open_fee - close_fee - funding_fee
            closed_at = datetime.now(timezone.utc).isoformat()

            await db.execute(
                '''UPDATE trades SET status='closed', exit_p=?, pnl=?, pnl_p=?,
                close_fee=?, funding_fee=?, net_pnl=?, closed_at=? WHERE coin=? AND status='TRADING' ''',
                (exit_price, gross_pnl, gross_pnl_p, close_fee, funding_fee, net_pnl, closed_at, coin)
            )
            await db.commit()
            return gross_pnl, gross_pnl_p, net_pnl

    async def get_open_trades(self) -> List[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE status='TRADING' OR status='STUCK'"
            )
            return await cursor.fetchall()

    async def get_closed_trades(self) -> List[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE status='closed' "
                "ORDER BY COALESCE(closed_at, created_at) ASC"
            )
            return await cursor.fetchall()
