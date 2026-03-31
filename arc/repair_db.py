import sqlite3
from parser import parse_signal

def repair():
    db_path = 'parsed_signals.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Выбираем ВСЕ записи, чтобы привести направление к LONG и заполнить пустоты
    cursor.execute("SELECT id, raw_text FROM signals")
    rows = cursor.fetchall()
    
    print(f"🔧 Начало обработки базы ({len(rows)} записей)...")
    
    repaired = 0
    for row_id, raw_text in rows:
        parsed = parse_signal(raw_text)
        if parsed and parsed['coin']:
            cursor.execute('''
                UPDATE signals SET 
                signal_type = ?, coin = ?, direction = ?, 
                entry_price = ?, target_price = ?, dca_price = ?, 
                close_price = ?, profit_pct = ?
                WHERE id = ?
            ''', (
                parsed['signal_type'], parsed['coin'], 'LONG', # Принудительный LONG
                parsed['entry_price'], parsed['target_price'], parsed['dca_price'],
                parsed['close_price'], parsed['profit_pct'], row_id
            ))
            repaired += 1
            
    conn.commit()
    conn.close()
    print(f"✅ База исправлена. Обновлено записей: {repaired}")

if __name__ == '__main__':
    repair()