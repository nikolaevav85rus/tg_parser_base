import sqlite3
import os
from datetime import datetime, timezone
from logger import bot_logger

DB_DIR = "db"
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)

class Database:
    def __init__(self, db_name=os.path.join(DB_DIR, "signals.db")):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row # Доступ по именам
        self.cursor = self.conn.cursor()
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, signal_type TEXT, 
            direction TEXT, price REAL, target_price REAL, received_at TEXT, raw_text TEXT)''')
        self.conn.commit()
        bot_logger.info("База данных сигналов (signals.db) успешно инициализирована.")

    def save_signal(self, data):
        bot_logger.debug(f"Попытка записи сигнала в БД: {data.get('coin')} | {data.get('signal_type')}")
        try:
            self.cursor.execute(
                "INSERT INTO signals (coin, signal_type, direction, price, target_price, received_at, raw_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (data['coin'], data['signal_type'], data.get('direction', 'LONG'), data['price'], data['target_price'], data['received_at'], data['raw_text'])
            )
            self.conn.commit()
            bot_logger.info(f"Сигнал {data['coin']} успешно сохранен в базу.")
            return True
        except Exception as e: 
            bot_logger.error(f"Критическая ошибка БД при сохранении сигнала {data.get('coin')}: {e}")
            return False

class TradesDatabase:
    def __init__(self, db_name=os.path.join(DB_DIR, "trades.db")):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row # Критически важно для стабильности
        self.cursor = self.conn.cursor()
        # Начальная схема без status (он добавится в upgrade)
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, step INTEGER, buy_p REAL, buy_a REAL, 
            dca1_p REAL, dca1_a REAL, dca2_p REAL, dca2_a REAL, dca3_p REAL, dca3_a REAL, 
            avg_p REAL, total_inv REAL, target_p REAL, created_at TEXT, 
            exit_p REAL, pnl REAL, pnl_p REAL)''')
        self.conn.commit()
        self._upgrade_db()

    def _upgrade_db(self):
        # Проверяем структуру таблицы через PRAGMA
        self.cursor.execute("PRAGMA table_info(trades)")
        existing_cols = [col[1] for col in self.cursor.fetchall()]
        
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
        
        for col_name, col_def in new_cols:
            if col_name not in existing_cols:
                try:
                    self.cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                    bot_logger.info(f"БД: Добавлена колонка {col_name}")
                except Exception as e:
                    bot_logger.error(f"БД: Ошибка при добавлении {col_name}: {e}")
        self.conn.commit()

    def create_trade(self, coin, entry_p, inv, target_p, leverage=1, open_fee=0.0):
        self.cursor.execute('''INSERT INTO trades (coin, step, buy_p, buy_a, avg_p, total_inv, status, target_p, created_at, leverage, open_fee) 
            VALUES (?, 0, ?, ?, ?, ?, 'TRADING', ?, ?, ?, ?)''', 
            (coin, entry_p, inv, entry_p, inv, target_p, datetime.now(timezone.utc).isoformat(), leverage, open_fee))
        self.conn.commit()

    def set_tp_order_id(self, coin: str, order_id: str | None):
        self.cursor.execute(
            "UPDATE trades SET tp_order_id=? WHERE coin=? AND status='TRADING'",
            (order_id, coin),
        )
        self.conn.commit()

    def set_dca_order_id(self, coin: str, order_id: str | None):
        self.cursor.execute(
            "UPDATE trades SET dca_order_id=? WHERE coin=? AND status='TRADING'",
            (order_id, coin),
        )
        self.conn.commit()

    def get_trading_trade(self, coin: str):
        self.cursor.execute("SELECT * FROM trades WHERE coin=? AND status='TRADING' LIMIT 1", (coin,))
        return self.cursor.fetchone()

    def update_trade_dca(self, coin, step, price, inv, fee=0.0):
        self.cursor.execute("SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'", (coin,))
        row = self.cursor.fetchone()
        if not row: return
        old_inv, old_avg, old_fee = row['total_inv'], row['avg_p'], row['open_fee']
        new_inv = old_inv + inv
        new_avg = ((old_inv / old_avg) * old_avg + inv) / ((old_inv / old_avg) + (inv / price)) if old_avg > 0 else price
        new_fee = old_fee + fee
        
        col_p, col_a = f"dca{step}_p", f"dca{step}_a"
        self.cursor.execute(f"UPDATE trades SET step=?, {col_p}=?, {col_a}=?, avg_p=?, total_inv=?, open_fee=? WHERE coin=? AND status='TRADING'", 
            (step, price, inv, new_avg, new_inv, new_fee, coin))
        self.conn.commit()

    def close_trade(self, coin, exit_price, close_fee=0.0, funding_fee=0.0):
        self.cursor.execute("SELECT total_inv, avg_p, open_fee FROM trades WHERE coin=? AND status='TRADING'", (coin,))
        row = self.cursor.fetchone()
        if not row: return 0, 0, 0
        inv, avg, open_fee = row['total_inv'], row['avg_p'], row['open_fee']
        qty = inv / avg if avg > 0 else 0
        
        gross_pnl = (qty * exit_price) - inv
        gross_pnl_p = (gross_pnl / inv * 100) if inv > 0 else 0
        net_pnl = gross_pnl - open_fee - close_fee - funding_fee
        
        self.cursor.execute('''UPDATE trades SET status='closed', exit_p=?, pnl=?, pnl_p=?, close_fee=?, funding_fee=?, net_pnl=? 
            WHERE coin=? AND status='TRADING' ''', (exit_price, gross_pnl, gross_pnl_p, close_fee, funding_fee, net_pnl, coin))
        self.conn.commit()
        return gross_pnl, gross_pnl_p, net_pnl

    def get_open_trades(self):
        self.cursor.execute("SELECT * FROM trades WHERE status='TRADING' OR status='STUCK'")
        return self.cursor.fetchall()

    def get_closed_trades(self):
        self.cursor.execute("SELECT * FROM trades WHERE status='closed'")
        return self.cursor.fetchall()

class SettingsDatabase:
    def __init__(self, db_name=os.path.join(DB_DIR, "settings.db")):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.commit()
        self.ensure_defaults()
        bot_logger.info("База данных настроек (settings.db) успешно инициализирована.")
        
    def get(self, key, default=None):
        self.cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = self.cursor.fetchone()
        if row and row['value'] is not None:
            return row['value']
        return default
        
    def set(self, key, value):
        self.cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()
        bot_logger.info(f"Параметр '{key}' был обновлен в БД. Новое значение: {value}")

    def ensure_defaults(self):
        """Проверяет и устанавливает значения по умолчанию для DCA, если они отсутствуют."""
        defaults = {
            "dca_0": "2",
            "dca_1": "4",
            "dca_2": "8",
            "dca_3": "16",
            "dca_level_1": "3.5",
            "dca_level_2": "6.5",
            "dca_level_3": "14.5",
            "tp_target": "1.5",
        }
        
        for key, default_value in defaults.items():
            self.cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
            if self.cursor.fetchone() is None:
                self.cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, default_value))
                bot_logger.info(f"БД Settings: Установлено значение по умолчанию для '{key}': {default_value}")
        self.conn.commit()


class CoinsDatabase:
    def __init__(self, db_name=os.path.join(DB_DIR, "coins.db")):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE IF NOT EXISTS coins (coin TEXT PRIMARY KEY, alias TEXT, is_active INTEGER)")
        self.conn.commit()
        
    def add_coin(self, coin, alias="", is_active=1):
        self.cursor.execute("INSERT OR IGNORE INTO coins (coin, alias, is_active) VALUES (?, ?, ?)", (coin, alias, is_active))
        self.conn.commit()
        
    def get_coin(self, coin):
        self.cursor.execute("SELECT coin, alias, is_active FROM coins WHERE coin=?", (coin,))
        row = self.cursor.fetchone()
        return {"coin": row['coin'], "alias": row['alias'], "is_active": bool(row['is_active'])} if row else None