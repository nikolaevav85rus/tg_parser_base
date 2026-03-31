import sqlite3

# Полный список торгуемых монет из оригинального бота
# Формат: ("Тикер в сигнале", "Алиас для биржи", Статус включения)
coins_to_add = [
    ("1INCHUSDT", "", 1),
    ("AAVEUSDT", "", 1),
    ("ADAUSDT", "", 1),
    ("ATOMUSDT", "", 1),
    ("AVAXUSDT", "", 1),
    ("AXSUSDT", "", 1),
    ("C98USDT", "", 1),
    ("DOTUSDT", "", 1),
    ("FTMUSDT", "", 1),
    ("KSMUSDT", "", 1),
    ("LUNAUSDT", "LUNA2USDT", 1), # Сразу прописываем правильный алиас для Bybit
    ("SOLUSDT", "", 1),
    ("XLMUSDT", "", 1),
    ("XTZUSDT", "", 1),
    # Оставил из прошлых сигналов на всякий случай:
    ("1000PEPEUSDT", "", 1),
    ("KASUSDT", "", 1)
]

def fill_coins_db(db_name="coins.db"):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    # На всякий случай убедимся, что таблица существует (если coins.db еще пуст)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS coins (
            coin TEXT PRIMARY KEY, 
            alias TEXT, 
            is_active INTEGER DEFAULT 0
        )
    ''')

    # INSERT OR REPLACE обновит запись, если монета уже есть, или создаст новую
    cursor.executemany('''
        INSERT OR REPLACE INTO coins (coin, alias, is_active) 
        VALUES (?, ?, ?)
    ''', coins_to_add)

    conn.commit()
    conn.close()
    
    print(f"✅ Успешно добавлено/обновлено {len(coins_to_add)} монет в базу {db_name}!")
    print("Обнови страницу настроек в браузере (F5), чтобы увидеть их в таблице.")

if __name__ == "__main__":
    fill_coins_db()