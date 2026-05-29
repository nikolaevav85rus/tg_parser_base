import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict

from logger import bot_logger
from database.base import BaseDatabase, DB_DIR


# Роли ордеров в trade_orders
ROLE_OPEN = 'OPEN'
ROLE_DCA = 'DCA'
ROLE_TP = 'TP'

# Статусы ордеров в trade_orders
ORDER_ACTIVE = 'ACTIVE'
ORDER_FILLED = 'FILLED'
ORDER_CANCELLED = 'CANCELLED'
ORDER_REPLACED = 'REPLACED'


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
            await db.execute('''
                CREATE TABLE IF NOT EXISTS trade_orders (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id   INTEGER NOT NULL,
                    order_id   TEXT NOT NULL,
                    role       TEXT NOT NULL,
                    step       INTEGER,
                    status     TEXT NOT NULL DEFAULT 'ACTIVE',
                    qty        REAL,
                    created_at TEXT,
                    FOREIGN KEY (trade_id) REFERENCES trades(id)
                )
            ''')
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_orders_trade "
                "ON trade_orders (trade_id, role, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_orders_orderid "
                "ON trade_orders (order_id)"
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
            ],
            3: []  # v3 не добавляет колонок в trades — только бэкфилл в trade_orders
        }

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            current_version = (row[0] if row else 0) or 0

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

                if version >= 3:
                    # Бэкфилл: для всех TRADING-сделок перенести tp_order_id / dca_order_id
                    # в новую таблицу trade_orders как ACTIVE
                    cur2 = await db.execute(
                        "SELECT id, step, tp_order_id, dca_order_id, created_at "
                        "FROM trades WHERE status='TRADING'"
                    )
                    backfilled = 0
                    for tr in await cur2.fetchall():
                        # Проверяем что для этой сделки записей ещё нет (идемпотентность)
                        check = await db.execute(
                            "SELECT COUNT(*) FROM trade_orders WHERE trade_id=?", (tr['id'],)
                        )
                        if (await check.fetchone())[0] > 0:
                            continue
                        ts = tr['created_at'] or datetime.now(timezone.utc).isoformat()
                        if tr['tp_order_id']:
                            await db.execute(
                                "INSERT INTO trade_orders "
                                "(trade_id, order_id, role, step, status, created_at) "
                                "VALUES (?, ?, 'TP', NULL, 'ACTIVE', ?)",
                                (tr['id'], tr['tp_order_id'], ts)
                            )
                            backfilled += 1
                        if tr['dca_order_id']:
                            # step следующего DCA = текущий step + 1
                            next_step = (tr['step'] or 0) + 1
                            await db.execute(
                                "INSERT INTO trade_orders "
                                "(trade_id, order_id, role, step, status, created_at) "
                                "VALUES (?, ?, 'DCA', ?, 'ACTIVE', ?)",
                                (tr['id'], tr['dca_order_id'], next_step, ts)
                            )
                            backfilled += 1
                    if backfilled:
                        bot_logger.info(f"БД миграция v3: бэкфилл trade_orders — {backfilled} записей")

                await db.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (version,)
                )
                bot_logger.info(f"БД: применена миграция v{version}")

            await db.commit()

    async def create_trade(
        self, coin: str, entry_p: float, inv: float, target_p: float,
        leverage: int = 1, open_fee: float = 0.0
    ) -> int:
        """Создаёт сделку, возвращает её id (lastrowid)."""
        async with self._connect() as db:
            cursor = await db.execute(
                '''INSERT INTO trades
                (coin, step, buy_p, buy_a, avg_p, total_inv, status, target_p, created_at, leverage, open_fee)
                VALUES (?, 0, ?, ?, ?, ?, 'TRADING', ?, ?, ?, ?)''',
                (coin, entry_p, inv, entry_p, inv, target_p,
                 datetime.now(timezone.utc).isoformat(), leverage, open_fee)
            )
            await db.commit()
            return cursor.lastrowid

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

    async def close_trade_realized(
        self, coin: str, exit_price: float, gross_pnl: float,
        net_pnl: float, funding_fee: float = 0.0
    ) -> Tuple[float, float, float]:
        """
        Закрытие сделки по РЕАЛЬНЫМ данным биржи (источник истины).
        gross_pnl — суммарная ценовая разница (Σ cumExit − cumEntry).
        net_pnl   — суммарный closedPnl с биржи (УЖЕ включает все комиссии И фандинг).
        funding_fee — фандинг отдельно (только для разбивки O/F/C в дашборде).

        Разбивка для дашборда (gross − O − F − C = net):
          open_fee  = 0
          close_fee = (gross − net) − funding   (= комиссии open+close)
          funding   = funding_fee
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT total_inv FROM trades WHERE coin=? AND status='TRADING'", (coin,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0.0, 0.0, 0.0

            inv = row['total_inv'] or 0.0
            gross_pnl_p = (gross_pnl / inv * 100) if inv > 0 else 0.0
            # Комиссии open+close = (gross − net) − funding. close_fee их агрегирует,
            # open_fee обнуляем, чтобы дашборд (O+F+C) не задвоил.
            close_fee = (gross_pnl - net_pnl) - funding_fee
            closed_at = datetime.now(timezone.utc).isoformat()

            await db.execute(
                '''UPDATE trades SET status='closed', exit_p=?, pnl=?, pnl_p=?,
                open_fee=0.0, close_fee=?, funding_fee=?, net_pnl=?, closed_at=?
                WHERE coin=? AND status='TRADING' ''',
                (exit_price, gross_pnl, gross_pnl_p, close_fee, funding_fee, net_pnl, closed_at, coin)
            )
            await db.commit()
            return gross_pnl, gross_pnl_p, net_pnl

    async def sync_position(
        self, coin: str, step: int, avg_p: float, total_inv: float, add_open_fee: float = 0.0
    ) -> None:
        """
        Синхронизирует состояние позиции с реальными данными биржи (avgPrice, positionValue).
        Используется при DCA вместо самостоятельного пересчёта средней цены.
        """
        async with self._connect() as db:
            await db.execute(
                "UPDATE trades SET step=?, avg_p=?, total_inv=?, open_fee=open_fee+? "
                "WHERE coin=? AND status='TRADING'",
                (step, avg_p, total_inv, add_open_fee, coin)
            )
            await db.commit()

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

    async def set_trade_status(self, coin: str, status: str) -> None:
        """Сменить статус активной сделки (например, TRADING → STUCK)."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE trades SET status=? "
                "WHERE coin=? AND status IN ('TRADING','STUCK')",
                (status, coin)
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Работа с trade_orders (множественные ордера на роль)
    # ------------------------------------------------------------------

    async def add_orders(
        self,
        trade_id: int,
        role: str,
        order_ids: List[str],
        step: Optional[int] = None,
        qty: Optional[float] = None,
    ) -> None:
        """Вставить набор order_id с заданной ролью и статусом ACTIVE."""
        if not order_ids:
            return
        ts = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.executemany(
                "INSERT INTO trade_orders "
                "(trade_id, order_id, role, step, status, qty, created_at) "
                "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
                [(trade_id, oid, role, step, qty, ts) for oid in order_ids]
            )
            await db.commit()

    async def get_active_orders(
        self, trade_id: int, role: Optional[str] = None
    ) -> List[aiosqlite.Row]:
        """Все ACTIVE-ордера сделки (опционально с фильтром по роли)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if role:
                cursor = await db.execute(
                    "SELECT * FROM trade_orders "
                    "WHERE trade_id=? AND role=? AND status='ACTIVE' "
                    "ORDER BY id ASC",
                    (trade_id, role)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM trade_orders "
                    "WHERE trade_id=? AND status='ACTIVE' "
                    "ORDER BY id ASC",
                    (trade_id,)
                )
            return await cursor.fetchall()

    async def mark_orders_status(
        self, order_ids: List[str], status: str
    ) -> None:
        """Пометить набор order_id новым статусом (FILLED / CANCELLED / REPLACED)."""
        if not order_ids:
            return
        async with self._connect() as db:
            placeholders = ",".join("?" for _ in order_ids)
            await db.execute(
                f"UPDATE trade_orders SET status=? WHERE order_id IN ({placeholders})",
                (status, *order_ids)
            )
            await db.commit()

    async def replace_active_orders(
        self,
        trade_id: int,
        role: str,
        new_order_ids: List[str],
        step: Optional[int] = None,
        qty: Optional[float] = None,
    ) -> List[str]:
        """
        Атомарная замена ACTIVE-ордеров заданной роли:
        старые ACTIVE → REPLACED, новые вставляются как ACTIVE.
        Возвращает список старых (REPLACED) order_id — их нужно отменить на бирже.
        """
        ts = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT order_id FROM trade_orders "
                "WHERE trade_id=? AND role=? AND status='ACTIVE'",
                (trade_id, role)
            )
            old_ids = [r['order_id'] for r in await cursor.fetchall()]

            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                await db.execute(
                    f"UPDATE trade_orders SET status='REPLACED' "
                    f"WHERE order_id IN ({placeholders})",
                    old_ids
                )

            if new_order_ids:
                await db.executemany(
                    "INSERT INTO trade_orders "
                    "(trade_id, order_id, role, step, status, qty, created_at) "
                    "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
                    [(trade_id, oid, role, step, qty, ts) for oid in new_order_ids]
                )

            await db.commit()
            return old_ids

    async def get_trade_id_by_coin(self, coin: str) -> Optional[int]:
        """ID активной сделки (TRADING/STUCK) по монете."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id FROM trades WHERE coin=? AND status IN ('TRADING','STUCK') LIMIT 1",
                (coin,)
            )
            row = await cursor.fetchone()
            return row['id'] if row else None
