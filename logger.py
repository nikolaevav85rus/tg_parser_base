import logging
import sys
import os
from datetime import datetime, timezone, timedelta

# Создаем папку для логов, если её нет
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

class GMT3Formatter(logging.Formatter):
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return (dt + timedelta(hours=3)).timetuple()

def setup_logger():
    logger = logging.getLogger("TradingBot")
    logger.setLevel(logging.DEBUG) 

    formatter = GMT3Formatter('[%(asctime)s] [%(levelname)s] [%(module)s] -> %(message)s', datefmt='%d.%m.%Y %H:%M:%S')

    # Путь к системному логу
    log_path = os.path.join(LOG_DIR, "bot_system.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG) 
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger

bot_logger = setup_logger()