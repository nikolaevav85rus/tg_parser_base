import sqlite3

def migrate():
    print("=== СТАРТ МИГРАЦИИ БАЗЫ ДАННЫХ ===")
    
    # 1. Обновляем базу сделок (trades.db)
    try:
        conn_t = sqlite3.connect('trades.db')
        cursor_t = conn_t.cursor()
        
        columns_to_add = [
            ("tp_order_id", "TEXT"),
            ("dca_order_id", "TEXT"),
            ("status", "TEXT DEFAULT 'TRADING'") 
        ]
        
        for col_name, col_type in columns_to_add:
            try:
                cursor_t.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                print(f"✅ Колонка {col_name} успешно добавлена в trades.db")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    print(f"ℹ️ Колонка {col_name} уже существует.")
                else:
                    print(f"⚠️ Ошибка при добавлении {col_name}: {e}")

        # ПЕРЕВОДИМ СТАРЫЕ СДЕЛКИ В НОВЫЙ СТАТУС
        cursor_t.execute("UPDATE trades SET status='TRADING' WHERE status='open'")
        
        conn_t.commit()
        conn_t.close()
        print("✅ Старые сделки успешно переведены в статус 'TRADING'!")
    except Exception as e:
        print(f"❌ Ошибка при обновлении trades.db: {e}")

    # 2. Обновляем настройки (settings.db)
    try:
        conn_s = sqlite3.connect('settings.db')
        cursor_s = conn_s.cursor()
        
        # Добавляем параметр допустимого ГЭПа
        res = cursor_s.execute("SELECT value FROM settings WHERE key='max_close_gap'").fetchone()
        if not res:
            cursor_s.execute("INSERT INTO settings (key, value) VALUES ('max_close_gap', '0.15')")
            print("✅ Параметр max_close_gap (0.15) добавлен в settings.db")
        else:
            print("ℹ️ Параметр max_close_gap уже настроен.")
            
        conn_s.commit()
        conn_s.close()
    except Exception as e:
        print(f"❌ Ошибка при обновлении settings.db: {e}")

    print("=== МИГРАЦИЯ ЗАВЕРШЕНА ===")

if __name__ == "__main__":
    migrate()