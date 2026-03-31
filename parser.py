import re
from logger import bot_logger
from datetime import datetime, timezone

def parse_signal(text):
    if not text: return None

    if 't.me/' in text or 'TL_indicator_bot' in text:
        bot_logger.info("Парсер: В сообщении обнаружена ссылка, продолжаем анализ...")

    clean_text = re.sub(r'[^\x00-\x7fа-яА-ЯёЁ\s\.:,/%#\|-]', '', text.replace('*', '').replace('_', ''))
    t_upper = clean_text.upper()

    coin_match = re.search(r'([A-ZА-Я0-9]+USDT)', t_upper)
    if not coin_match: return None
    
    coin = coin_match.group(1)
    ru_to_en = {'А':'A', 'В':'B', 'Е':'E', 'К':'K', 'М':'M', 'Н':'H', 'О':'O', 'Р':'P', 'С':'C', 'Т':'T', 'Х':'X', 'У':'Y'}
    for cyr, lat in ru_to_en.items(): coin = coin.replace(cyr, lat)

    header_match = re.search(r'\|\s*([А-ЯA-Z0-9]+)', t_upper)
    if not header_match: return None
    header_action = header_match.group(1)
    
    # Пропускаем все, что не является сигналом на открытие
    if not any(x in header_action for x in ['ОКУПК', 'OPEN', 'LONG']):
        bot_logger.info(f"Парсер: Игнорируется сигнал '{header_action}' для {coin}, так как бот работает в автономном режиме и принимает только команды на открытие.")
        return None

    signal_type = 'OPEN'

    price = None
    p_m = re.search(r'(?:упить|окупка)[^\d]*([0-9.]+)', clean_text, re.I)
    if p_m: price = float(p_m.group(1))

    t_m = re.search(r'родать[^\d]*([0-9.]+)', clean_text, re.I)
    target_p = float(t_m.group(1)) if t_m else None

    bot_logger.info(f"Парсер: Разобран сигнал {coin} | {signal_type} | Цена: {price}")
    
    return {
        'coin': coin, 
        'signal_type': signal_type, 
        'direction': 'LONG', 
        'price': price, 
        'target_price': target_p,
        'received_at': datetime.now(timezone.utc).isoformat(),
        'raw_text': text
    }
