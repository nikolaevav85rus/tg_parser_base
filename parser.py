import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from logger import bot_logger

# Предкомпиляция регулярных выражений для максимальной производительности
COIN_PATTERN = re.compile(r'([A-ZА-Я0-9]+USDT)')
HEADER_PATTERN = re.compile(r'\|\s*([А-ЯA-Z0-9]+)')
PRICE_PATTERN = re.compile(r'(?:упить|окупка)[^\d]*([0-9.]+)', re.IGNORECASE)
TARGET_PATTERN = re.compile(r'родать[^\d]*([0-9.]+)', re.IGNORECASE)

# Таблица трансляции для быстрой замены кириллических символов на латинские в тикерах
CYR_TO_LAT_TABLE = str.maketrans(
    'АВЕКМНОРСТХУ', 
    'ABEKMHOPCTXY'
)


def _safe_float(value: str) -> Optional[float]:
    """Безопасное преобразование строки в число с плавающей точкой."""
    try:
        # Убираем возможные лишние точки в конце (например, если парсер зацепил конец предложения)
        return float(value.rstrip('.'))
    except (ValueError, TypeError):
        return None


def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    """
    Парсинг текста сообщения для извлечения торгового сигнала.
    Чистая функция (Pure Function) — не выполняет I/O операций.
    
    :param text: Сырой текст сообщения из Telegram.
    :return: Словарь с данными сигнала или None, если сигнал не распознан.
    """
    if not text: 
        return None

    if 't.me/' in text or 'TL_indicator_bot' in text:
        bot_logger.debug("Парсер: В сообщении обнаружена ссылка, продолжаем анализ...")

    # Очистка текста от мусора, сохраняем только нужные символы
    clean_text = re.sub(r'[^\x00-\x7fа-яА-ЯёЁ\s\.:,/%#\|-]', '', text.replace('*', '').replace('_', ''))
    t_upper = clean_text.upper()

    # 1. Поиск тикера монеты
    coin_match = COIN_PATTERN.search(t_upper)
    if not coin_match: 
        return None
    
    # Быстрая замена кириллических "обманок" на латиницу (например, С в кириллице -> C в латинице)
    coin = coin_match.group(1).translate(CYR_TO_LAT_TABLE)

    # 2. Поиск заголовка действия (OPEN, LONG, ОКУПКА)
    header_match = HEADER_PATTERN.search(t_upper)
    if not header_match: 
        return None
        
    header_action = header_match.group(1)
    if not any(x in header_action for x in ['ОКУПК', 'OPEN', 'LONG']):
        bot_logger.info(f"Парсер: Игнорируется сигнал '{header_action}' для {coin} (не лонг/открытие).")
        return None

    signal_type = 'OPEN'

    # 3. Извлечение цен
    price = None
    price_match = PRICE_PATTERN.search(clean_text)
    if price_match: 
        price = _safe_float(price_match.group(1))

    target_price = None
    target_match = TARGET_PATTERN.search(clean_text)
    if target_match: 
        target_price = _safe_float(target_match.group(1))

    # Логирование результата
    bot_logger.info(f"Парсер: Успешно разобран сигнал {coin} | {signal_type} | Вход: {price} | Цель: {target_price}")
    
    return {
        'coin': coin, 
        'signal_type': signal_type, 
        'direction': 'LONG', 
        'price': price, 
        'target_price': target_price,
        'received_at': datetime.now(timezone.utc).isoformat(),
        'raw_text': text
    }