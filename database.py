import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from logger import bot_logger

DB_DIR = "db"
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)

class Database:
    """База данных для хранения входящих сигналов."""
    def __init__(self, db_name: str = os.path.join(DB_DIR, "signals.db")):
        self.db_name = db_name

    async def init_db(self) -> None:
        """Асинхронная инициализация таблиц."""
        async with aiosqlite.connect(self.db_name) as db:
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
            await db.commit()
        bot_logger.info("База данных сигналов (signals.db) успешно инициализирована.")

    async def save_signal(self, data: Dict[str, Any]) -> bool:
        """Сохранение распарсенного сигнала."""
        bot_logger.debug(f"Попытка записи сигнала в БД: {data.get('coin')} | {data.get('signal_type')}")
        try:
            async with aiosqlite.connect(self.db_name) as db:
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


class TradesDatabase:
    """База данных для управления торговыми сделками (позициями)."""
    def __init__(self, db_name: str = os.path.join(DB_DIR, "trades.db")):
        self.db_name = db_name

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    coin TEXT, step INTEGER, buy_p REAL, buy_a REAL, 
                    dca1_p REAL, dca1_a REAL, dca2_p REAL, dca2_a REAL, dca3_p REAL, dca3_a REAL, 
                    avg_p REAL, total_inv REAL, target_p REAL, created_at TEXT, 
                    exit_p REAL, pnl REAL, pnl_p REAL
                )
            ''')
            await db.commit()
        await self._upgrade_db()

    async def _upgrade_db(self) -> None:
        """Миграция структуры таблицы (добавление новых колонок)."""
        new_cols = [
            ("leverage", "INTEGER DEFAULT 1"), 
            ("open_fee", "REAL DEFAULT 0.0"), 
            ("funding_fee", "REAL DEFAULT 0.0"), 
            ("close_fee", "REAL DEFAULT 0.0"), 
            ("net_pnl", "REAL DEFAULT 0.0"),
            ("tp_order_id", "TEXT"), 
            ("dca_order_id", "TEXT"), 
            ("status", "TEXT DEFAULT 'TRADING'")
        ]
        
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("PRAGMA table_info(trades)")
            rows = await cursor.fetchall()
            existing_cols = [row[1] for row in rows]
            
            for col_name, col_def in new_cols:
                if col_name not in existing_cols:
                    try:
                        await db.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                        bot_logger.info(f"БД: Добавлена колонка {col_name}")
                    except Exception as e:
                        bot_logger.error(f"БД: Ошибка при добавлении {col_name}: {e}")
            await db.commit()

    async def create_trade(self, coin: str, entry_p: float, inv: float, target_p: float, leverage: int = 1, open_fee: float = 0.0) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                '''INSERT INTO trades 
                (coin, step, buy_p, buy_a, avg_p, total_inv, status, target_p, created_at, leverage, open_fee) 
                VALUES (?, 0, ?, ?, ?, ?, 'TRADING', ?, ?, ?, ?)''', 
                (coin, entry_p, inv, entry_p, inv, target_p, datetime.now(timezone.utc).isoformat(), leverage, open_fee)
            )
            await db.commit()

    async def set_tp_order_id(self, coin: str, order_id: Optional[str]) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE trades SET tp_order_id=? WHERE coin=? AND status='TRADING'",
                (order_id, coin)
            )
            await db.commit()

    async def set_dca_order_id(self, coin: str, order_id: Optional[str]) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE trades SET dca_order_id=? WHERE coin=? AND status='TRADING'",
                (order_id, coin)
            )
            await db.commit()

    async def get_trading_trade(self, coin: str) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades WHERE coin=? AND status='TRADING' LIMIT 1", (coin,))
            return await cursor.fetchone()

    async def update_trade_dca(self, coin: str, step: int, price: float, inv: float, fee: float = 0.0) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'", (coin,))
            row = await cursor.fetchone()
            
            if not row: 
                return
                
            old_inv, old_avg, old_fee = row['total_inv'], row['avg_p'], row['open_fee']
            new_inv = old_inv + inv
            # Защита от деления на ноль, если старая цена была 0
            if old_avg > 0:
                new_avg = ((old_inv / old_avg) * old_avg + inv) / ((old_inv / old_avg) + (inv / price))
            else:
                new_avg = price
                
            new_fee = old_fee + fee
            col_p, col_a = f"dca{step}_p", f"dca{step}_a"
            
            await db.execute(
                f"UPDATE trades SET step=?, {col_p}=?, {col_a}=?, avg_p=?, total_inv=?, open_fee=? WHERE coin=? AND status='TRADING'", 
                (step, price, inv, new_avg, new_inv, new_fee, coin)
            )
            await db.commit()

    async def close_trade(self, coin: str, exit_price: float, close_fee: float = 0.0, funding_fee: float = 0.0) -> Tuple[float, float, float]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'", (coin,))
            row = await cursor.fetchone()
            
            if not row: 
                return 0.0, 0.0, 0.0
                
            inv, avg, open_fee = row['total_inv'], row['avg_p'], row['open_fee']
            qty = inv / avg if avg > 0 else 0
            
            gross_pnl = (qty * exit_price) - inv
            gross_pnl_p = (gross_pnl / inv * 100) if inv > 0 else 0
            net_pnl = gross_pnl - open_fee - close_fee - funding_fee
            
            await db.execute('''
                UPDATE trades SET status='closed', exit_p=?, pnl=?, pnl_p=?, close_fee=?, funding_fee=?, net_pnl=? 
                WHERE coin=? AND status='TRADING' 
            ''', (exit_price, gross_pnl, gross_pnl_p, close_fee, funding_fee, net_pnl, coin))
            await db.commit()
            
            return gross_pnl, gross_pnl_p, net_pnl

    async def get_open_trades(self) -> List[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades WHERE status='TRADING' OR status='STUCK'")
            return await cursor.fetchall()

    async def get_closed_trades(self) -> List[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM trades WHERE status='closed'")
            return await cursor.fetchall()


class SettingsDatabase:
    """База данных для хранения настроек бота (DCA, TP)."""
    def __init__(self, db_name: str = os.path.join(DB_DIR, "settings.db")):
        self.db_name = db_name

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            await db.commit()
        await self.ensure_defaults()
        bot_logger.info("База данных настроек (settings.db) успешно инициализирована.")
        
    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cursor.fetchone()
            if row and row['value'] is not None:
                return row['value']
            return default
        
    async def set(self, key: str, value: Any) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            await db.commit()
        bot_logger.info(f"Параметр '{key}' был обновлен в БД. Новое значение: {value}")

    async def ensure_defaults(self) -> None:
        """Проверяет и устанавливает значения по умолчанию для DCA, если они отсутствуют."""
        defaults = {
            "dca_0": "2", "dca_1": "4", "dca_2": "8", "dca_3": "16",
            "dca_level_1": "3.5", "dca_level_2": "6.5", "dca_level_3": "14.5",
            "tp_target": "1.5",
        }
        
        async with aiosqlite.connect(self.db_name) as db:
            for key, default_value in defaults.items():
                cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
                if await cursor.fetchone() is None:
                    await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, default_value))
                    bot_logger.info(f"БД Settings: Установлено значение по умолчанию для '{key}': {default_value}")
            await db.commit()


class CoinsDatabase:
    """База данных для управления списком активных монет."""
    def __init__(self, db_name: str = os.path.join(DB_DIR, "coins.db")):
        self.db_name = db_name

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS coins (coin TEXT PRIMARY KEY, alias TEXT, is_active INTEGER)")
            await db.commit()
        
    async def add_coin(self, coin: str, alias: str = "", is_active: int = 1) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR IGNORE INTO coins (coin, alias, is_active) VALUES (?, ?, ?)", (coin, alias, is_active))
            await db.commit()
        
    async def get_coin(self, coin: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT coin, alias, is_active FROM coins WHERE coin=?", (coin,))
            row = await cursor.fetchone()
            return {"coin": row['coin'], "alias": row['alias'], "is_active": bool(row['is_active'])} if row else None