import sqlite3
import re
from datetime import datetime, timezone
from telethon import TelegramClient, events

# --- ТВОИ ДАННЫЕ ---
# Замени эти значения на свои
api_id = 38820955  # Твой api_id (число, без кавычек)
api_hash = 'dcf7f5b98747594bee3de1839660463c'  # Строка в кавычках
phone_number = '+79889456234' # Твой номер телефона в международном формате

# Публичный канал для теста (можешь вписать любой другой, например '@durov')
TARGET_CHANNEL = -1866869443 #'https://t.me/txt_mdk' 

# --- 1. БАЗА ДАННЫХ ---
conn = sqlite3.connect('parsed_signals.db', check_same_thread=False)
cursor = conn.cursor()

# Создаем расширенную таблицу под все параметры
cursor.execute('''
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT,
        signal_type TEXT,      -- OPEN или CLOSE
        coin TEXT,             -- например, DOTUSDT
        direction TEXT,        -- LONG или SHORT
        entry_price REAL,      -- Цена входа (для OPEN)
        target_price REAL,     -- Цена тейк-профита (для OPEN)
        dca_price REAL,        -- Цена усреднения (для OPEN)
        close_price REAL,      -- Цена закрытия (для CLOSE)
        profit_pct REAL,       -- Процент профита (для CLOSE)
        raw_text TEXT          -- Исходный текст на всякий случай
    )
''')
conn.commit()

# --- 2. ЛОГИКА ПАРСИНГА ---
def parse_signal(text):
    # Проверяем, наш ли это формат (есть ли маркеры)
    if '🚥' not in text:
        return None
        
    data = {
        'signal_type': None,
        'coin': None,
        'direction': 'LONG' if '⬆' in text else ('SHORT' if '⬇' in text else None),
        'entry_price': None,
        'target_price': None,
        'dca_price': None,
        'close_price': None,
        'profit_pct': None
    }

    # Ищем монету и тип сигнала (ПОКУПКА/OPENING или ЗАКРЫТИЕ/CLOSING)
    header_match = re.search(r'(?:⬆|⬇)\s*([A-Z]+)\s*\|\s*(ПОКУПКА|OPENING|ЗАКРЫТИЕ|CLOSING)', text)
    if header_match:
        data['coin'] = header_match.group(1)
        action = header_match.group(2)
        if action in ['ПОКУПКА', 'OPENING']:
            data['signal_type'] = 'OPEN'
        elif action in ['ЗАКРЫТИЕ', 'CLOSING']:
            data['signal_type'] = 'CLOSE'

    # Парсим цены в зависимости от типа сигнала
    if data['signal_type'] == 'OPEN':
        entry = re.search(r'(?:Купить|Buy):\s*([\d\.]+)', text)
        target = re.search(r'(?:Продать|Sell):\s*([\d\.]+)', text)
        dca = re.search(r'(?:Усред_1|Aver 1):\s*([\d\.]+)', text)
        
        if entry: data['entry_price'] = float(entry.group(1))
        if target: data['target_price'] = float(target.group(1))
        if dca: data['dca_price'] = float(dca.group(1))

    elif data['signal_type'] == 'CLOSE':
        close_p = re.search(r'(?:Выход из позиции|Closed by):\s*([\d\.]+)', text)
        profit = re.search(r'(?:Прибыль|Profit)\s*([\d\.]+)%', text)
        
        if close_p: data['close_price'] = float(close_p.group(1))
        if profit: data['profit_pct'] = float(profit.group(1))

    return data

# --- 3. ТЕЛЕГРАМ КЛИЕНТ ---
client = TelegramClient('partner_session', api_id, api_hash)

@client.on(events.NewMessage(chats=TARGET_CHANNEL))
async def new_message_handler(event):
    if not event.message.message:
        return

    text = event.message.message
    parsed_data = parse_signal(text)
    
    # Если парсер ничего не нашел (не наш формат), просто игнорируем
    if not parsed_data:
        return

    received_at = datetime.now(timezone.utc).isoformat()

    cursor.execute('''
        INSERT INTO signals (
            received_at, signal_type, coin, direction, 
            entry_price, target_price, dca_price, 
            close_price, profit_pct, raw_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        received_at, parsed_data['signal_type'], parsed_data['coin'], parsed_data['direction'],
        parsed_data['entry_price'], parsed_data['target_price'], parsed_data['dca_price'],
        parsed_data['close_price'], parsed_data['profit_pct'], text
    ))
    conn.commit()

    print(f"[{received_at}] Сохранен {parsed_data['signal_type']} сигнал для {parsed_data['coin']}")

# --- ЗАПУСК ---
print("Бот запущен. Парсер активен...")
client.start()
client.run_until_disconnected()