import asyncio
from database import Database, TradesDatabase
from arc.exchange import PaperExchange
from parser import parse_signal
import config

async def run_test_chain():
    # Инициализация
    db = Database()
    trades_db = TradesDatabase()
    exchange = PaperExchange(config.DEPO_USDT, trades_db)
    
    # Подгружаем текущие позиции (если бот уже что-то держит)
    exchange.load_active_positions()

    # Цепочка сигналов
    test_signals = [
        {
            "desc": "🚀 ОТКРЫТИЕ ПОЗИЦИИ (1%)",
            "text": "🚥\n🟢 BTCUSDT     |    **ПОКУПКА**\n\nКупить:  **65000.00**\nПродать:  **67000.00**\n\n#BTCUSDT"
        },
        {
            "desc": "📉 ПЕРВОЕ УСРЕДНЕНИЕ (2%)",
            "text": "🚥\n🟢 BTCUSDT     |    **УСРЕД_1**\n\nКупить:  **63000.00**\nПродать:  **67000.00**\n\n#BTCUSDT"
        },
        {
            "desc": "📉 ВТОРОЕ УСРЕДНЕНИЕ (4%)",
            "text": "🚥\n🟢 BTCUSDT     |    **УСРЕД_2**\n\nКупить:  **60000.00**\nПродать:  **67000.00**\n\n#BTCUSDT"
        },
        {
            "desc": "📉 ТРЕТЬЕ УСРЕДНЕНИЕ (8%)",
            "text": "🚥\n🟢 BTCUSDT     |    **УСРЕД_3**\n\nКупить:  **55000.00**\nПродать:  **67000.00**\n\n#BTCUSDT"
        },
        {
            "desc": "🏁 ЗАКРЫТИЕ ПОЗИЦИИ",
            "text": "🚥\n🔴 BTCUSDT     |    **ЗАКРЫТИЕ**\n\nВыход из позиции:  **62000.00**\n\n#BTCUSDT"
        }
    ]

    print(f"🧪 Запуск тестовой цепочки для BTCUSDT (всего {len(test_signals)} шагов)")
    print("⏱ Пауза между шагами: 60 секунд\n")

    for i, step in enumerate(test_signals):
        print(f"[{i+1}/{len(test_signals)}] {step['desc']}")
        
        p = parse_signal(step['text'])
        if p:
            # Выполняем сигнал
            await exchange.execute_signal(p['coin'], p['signal_type'], p['price'], p['target_price'])
            print(f"✅ Сигнал обработан. Баланс в системе: ${exchange.balance:.2f}")
        else:
            print("❌ Ошибка парсинга сигнала!")

        if i < len(test_signals) - 1:
            print("⏳ Ждем 60 секунд до следующего сообщения...")
            await asyncio.sleep(60)

    print("\n🏆 Тестовая цепочка завершена!")

if __name__ == "__main__":
    asyncio.run(run_test_chain())