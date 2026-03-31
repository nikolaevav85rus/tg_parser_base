import sqlite3

# Обновляем базу сигналов (signals.db)
try:
    conn = sqlite3.connect('signals.db')
    cursor = conn.cursor()
    cursor.execute("ALTER TABLE signals ADD COLUMN direction TEXT DEFAULT 'LONG'")
    conn.commit()
    print("✅ Колонка 'direction' успешно добавлена в таблицу signals!")
except Exception as e:
    # Если колонка уже есть, SQLite выдаст ошибку
    if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
        print("ℹ️ Колонка 'direction' уже существует.")
    else:
        print(f"⚠️ Ошибка: {e}")
finally:
    conn.close()