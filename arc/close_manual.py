import sqlite3

def close_active_trades():
    print("=== СИНХРОНИЗАЦИЯ БАЗЫ ДАННЫХ И БИРЖИ ===")
    try:
        conn = sqlite3.connect('trades.db')
        cursor = conn.cursor()
        
        # Ищем все открытые сделки
        cursor.execute("SELECT coin FROM trades WHERE status IN ('TRADING', 'open', 'STUCK')")
        open_trades = cursor.fetchall()
        
        if not open_trades:
            print("ℹ️ В базе данных нет открытых сделок. Всё чисто!")
            return

        print(f"Найдено сделок для закрытия: {len(open_trades)}")
        for t in open_trades:
            print(f"🔸 Закрываем в базе: {t[0]}")

        # Принудительно меняем статус на closed
        cursor.execute("UPDATE trades SET status='closed' WHERE status IN ('TRADING', 'open', 'STUCK')")
        conn.commit()
        
        print("✅ Все сделки успешно переведены в статус 'closed'!")
        
    except Exception as e:
        print(f"❌ Ошибка при обновлении trades.db: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    close_active_trades()