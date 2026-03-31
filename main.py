import asyncio, uvicorn, sys, os
from telethon import TelegramClient, events
import config
from parser import parse_signal
from database import Database, TradesDatabase, SettingsDatabase, CoinsDatabase
from bybit_exchange import BybitExchange 
from web_server import app, set_context
from notifier import Notifier
from datetime import datetime, timezone, timedelta
from logger import bot_logger

trade_lock = asyncio.Lock()

db = Database()
trades_db = TradesDatabase()
settings_db = SettingsDatabase()
coins_db = CoinsDatabase()
settings_db.ensure_defaults()

bot_notifier = Notifier(trades_db, settings_db)
exchange = BybitExchange(config.DEPO_USDT, trades_db, settings_db, coins_db, bot_notifier)
bot_notifier.set_exchange(exchange)
set_context(db, exchange, trades_db, settings_db, coins_db)

client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
bot_notifier.set_tg_client(client)

@client.on(events.NewMessage(chats=config.TARGET_CHANNEL))
async def handler(event):
    if not event.message.message: return
    
    # 1. ЛОГИРУЕМ СРАЗУ (вне блокировки)
    time_now = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%H:%M:%S')
    bot_logger.info(f"🔔 [{time_now}] Получено сообщение TG (ID: {event.message.id})")

    # 2. ОЧЕРЕДЬ НА ОБРАБОТКУ
    async with trade_lock:
        try:
            p = parse_signal(event.message.message)
            if not p: return
            p['received_at'] = datetime.now(timezone.utc).isoformat()
            p['raw_text'] = event.message.message
            if db.save_signal(p):
                await exchange.execute_signal(p['coin'], p['signal_type'], p['price'], p['target_price'])
        except Exception as e: bot_logger.error(f"Ошибка в handler: {e}")

async def main():
    exchange.load_active_positions()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning"))
    
    bot_logger.info("🚀 Запуск всех систем бота...")
    try:
        # Запускаем задачи
        await asyncio.gather(
            server.serve(), 
            client.start(), 
            exchange.monitor_fills(),
            bot_notifier.start_polling()
        )
    except asyncio.CancelledError:
        # Это нормально при остановке gather
        pass
    except Exception as e:
        bot_logger.error(f"Критическая ошибка в main loop: {e}")
    finally:
        bot_logger.info("📴 Системы останавливаются...")
        await client.disconnect()
        bot_logger.info("🛑 РАБОТА ЗАВЕРШЕНА: Бот полностью остановлен.")

if __name__ == '__main__':
    if sys.platform == "win32": 
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ловим Ctrl+C здесь, чтобы не выводить Traceback в консоль
        pass