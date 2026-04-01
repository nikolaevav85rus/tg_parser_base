import logging
import sys
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from typing import Optional

# Создаем папку для логов, если её нет
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)


class GMT3Formatter(logging.Formatter):
    """Кастомный форматер для принудительного использования времени GMT+3 (МСК)."""
    
    def converter(self, timestamp: float) -> tuple:  # type: ignore
        """Конвертация UTC timestamp в кортеж времени с нужным смещением."""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return (dt + timedelta(hours=3)).timetuple()


def setup_logger() -> logging.Logger:
    """
    Инициализация и настройка глобального логгера приложения.
    Использует ротацию файлов для предотвращения переполнения диска.
    """
    logger = logging.getLogger("TradingBot")
    logger.setLevel(logging.DEBUG) 

    # Предотвращаем дублирование обработчиков при повторном импорте модуля
    if logger.handlers:
        return logger

    formatter = GMT3Formatter(
        fmt='[%(asctime)s] [%(levelname)s] [%(module)s] -> %(message)s', 
        datefmt='%d.%m.%Y %H:%M:%S'
    )

    # Файловый обработчик с РОТАЦИЕЙ
    # maxBytes=5242880 (5 МБ) - при достижении этого размера создается новый файл
    # backupCount=5 - храним максимум 5 старых файлов (bot_system.log.1, bot_system.log.2 и т.д.)
    log_path = os.path.join(LOG_DIR, "bot_system.log")
    file_handler = RotatingFileHandler(
        log_path, 
        mode='a', 
        maxBytes=5 * 1024 * 1024, 
        backupCount=5, 
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG) 
    file_handler.setFormatter(formatter)

    # Консольный обработчик
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

# Глобальный инстанс логгера для импорта в другие модули
bot_logger = setup_logger()