import os
import sys
from dotenv import load_dotenv

# 1. Определяем базовую директорию проекта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Определяем путь к папке config
CONFIG_DIR = os.path.join(BASE_DIR, "config")
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

# 3. Ищем и загружаем файл .env
env_path_config = os.path.join(CONFIG_DIR, ".env")
env_path_root = os.path.join(BASE_DIR, ".env")

if os.path.exists(env_path_config):
    load_dotenv(env_path_config)
elif os.path.exists(env_path_root):
    load_dotenv(env_path_root)
else:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Файл .env не найден ни в папке config, ни в корне проекта!")
    sys.exit(1)

# ==========================================
# НАСТРОЙКИ TELEGRAM (ПАРСЕР СИГНАЛОВ)
# ==========================================
# ИСПРАВЛЕНО: Теперь ищем переменные с префиксом TG_, как у тебя в .env
_api_id = os.getenv("TG_API_ID")
API_ID = int(_api_id) if _api_id and _api_id.isdigit() else None
API_HASH = os.getenv("TG_API_HASH", "")

# Файл сессии будет сохраняться в папку config/
session_filename = os.getenv("SESSION_NAME", "session_settings")
SESSION_NAME = os.path.join(CONFIG_DIR, session_filename)

_target = os.getenv("TG_TARGET_CHANNEL", "")
try:
    # Пытаемся преобразовать ID канала в число
    TARGET_CHANNEL = int(_target)
except ValueError:
    # Если там указан текстовый юзернейм, оставляем как текст
    TARGET_CHANNEL = _target

# ==========================================
# НАСТРОЙКИ УВЕДОМИТЕЛЯ (ЛИЧНЫЙ БОТ)
# ==========================================
TG_NOTIFIER_TOKEN = os.getenv("TG_NOTIFIER_TOKEN", "")
TG_ADMIN_ID = os.getenv("TG_ADMIN_ID", "")

_allowed_str = os.getenv("ALLOWED_USERS", "")
if _allowed_str:
    ALLOWED_USERS = [int(x.strip()) for x in _allowed_str.split(",") if x.strip().lstrip('-').isdigit()]
else:
    ALLOWED_USERS = [int(TG_ADMIN_ID)] if TG_ADMIN_ID and TG_ADMIN_ID.lstrip('-').isdigit() else []

# ==========================================
# НАСТРОЙКИ БИРЖИ BYBIT
# ==========================================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "False").lower() in ("true", "1", "t", "yes")

# ==========================================
# ТОРГОВЫЕ НАСТРОЙКИ ПО УМОЛЧАНИЮ
# ==========================================
DEPO_USDT = float(os.getenv("DEPO_USDT", "100.0"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TRADE_PERCENT_1 = float(os.getenv("TRADE_PERCENT_1", "2.0"))
TRADE_PERCENT_2 = float(os.getenv("TRADE_PERCENT_2", "4.0"))
TRADE_PERCENT_4 = float(os.getenv("TRADE_PERCENT_4", "8.0"))
TRADE_PERCENT_8 = float(os.getenv("TRADE_PERCENT_8", "16.0"))