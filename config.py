import os
import sys
from pathlib import Path
from typing import List, Union
from dotenv import load_dotenv

# 1. Определяем базовые директории через современный pathlib
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_DIR.mkdir(exist_ok=True)

# 2. Ищем и загружаем файл .env
env_path_config = CONFIG_DIR / ".env"
env_path_root = BASE_DIR / ".env"

if env_path_config.exists():
    load_dotenv(env_path_config)
elif env_path_root.exists():
    load_dotenv(env_path_root)
else:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Файл .env не найден ни в папке config, ни в корне проекта!")
    sys.exit(1)

# ==========================================
# ХЕЛПЕРЫ ДЛЯ БЕЗОПАСНОГО ПАРСИНГА .ENV
# ==========================================

def _get_int(key: str, default: int = 0) -> int:
    """Безопасное извлечение целого числа."""
    val = os.getenv(key)
    return int(val) if val and val.lstrip('-').isdigit() else default

def _get_float(key: str, default: float = 0.0) -> float:
    """Безопасное извлечение числа с плавающей точкой."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _get_bool(key: str, default: bool = False) -> bool:
    """Преобразование строкового значения в boolean."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "t", "yes")

def _parse_allowed_users() -> List[int]:
    """Парсинг списка ID пользователей/админов Telegram."""
    users_str = os.getenv("ALLOWED_USERS", "")
    if users_str:
        # Обработка как положительных ID (пользователи), так и отрицательных (супергруппы)
        return [int(x.strip()) for x in users_str.split(",") if x.strip().lstrip('-').isdigit()]
    
    admin_id = os.getenv("TG_ADMIN_ID", "")
    return [int(admin_id)] if admin_id.lstrip('-').isdigit() else []

def _parse_target_channel() -> Union[int, str]:
    """Определение типа целевого канала (текстовый alias или числовой ID)."""
    target = os.getenv("TG_TARGET_CHANNEL", "")
    if target.lstrip('-').isdigit():
        return int(target)
    return target

# ==========================================
# НАСТРОЙКИ TELEGRAM (ПАРСЕР СИГНАЛОВ)
# ==========================================
API_ID: int = _get_int("TG_API_ID")
if not API_ID:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не задан обязательный параметр TG_API_ID в .env!")
    sys.exit(1)

API_HASH: str = os.getenv("TG_API_HASH", "")
TARGET_CHANNEL: Union[int, str] = _parse_target_channel()

# Файл сессии будет сохраняться в папку config/
_session_filename = os.getenv("SESSION_NAME", "session_settings")
SESSION_NAME: str = str(CONFIG_DIR / _session_filename)

# ==========================================
# НАСТРОЙКИ УВЕДОМИТЕЛЯ (ЛИЧНЫЙ БОТ)
# ==========================================
TG_NOTIFIER_TOKEN: str = os.getenv("TG_NOTIFIER_TOKEN", "")
TG_ADMIN_ID: str = os.getenv("TG_ADMIN_ID", "")
ALLOWED_USERS: List[int] = _parse_allowed_users()

# ==========================================
# НАСТРОЙКИ БИРЖИ BYBIT
# ==========================================
BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET: bool = _get_bool("BYBIT_TESTNET", False)

# ==========================================
# ТОРГОВЫЕ НАСТРОЙКИ ПО УМОЛЧАНИЮ
# ==========================================
DEPO_USDT: float = _get_float("DEPO_USDT", 2000.0)
LEVERAGE: int = _get_int("LEVERAGE", 2)
TRADE_PERCENT_1: float = _get_float("TRADE_PERCENT_1", 2.0)
TRADE_PERCENT_2: float = _get_float("TRADE_PERCENT_2", 4.0)
TRADE_PERCENT_4: float = _get_float("TRADE_PERCENT_4", 8.0)
TRADE_PERCENT_8: float = _get_float("TRADE_PERCENT_8", 16.0)