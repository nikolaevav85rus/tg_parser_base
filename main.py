import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import uvicorn
from telethon import TelegramClient, events

# Локальные импорты
import config
from parser import parse_signal
from database import Database, TradesDatabase, SettingsDatabase, CoinsDatabase
from bybit_exchange import BybitExchange 
from web_server import app, set_context
from notifier import Notifier
from logger import bot_logger


class TradingBot:
    """
    Основной класс приложения, инкапсулирующий состояние и жизненный цикл бота.
    """
    def __init__(self) -> None:
        # Инициализация экземпляров баз данных (без подключения, так как оно теперь асинхронное)
        self.db = Database()
        self.trades_db = TradesDatabase()
        self.settings_db = SettingsDatabase()
        self.coins_db = CoinsDatabase()

        # Инициализация сервисов
        self.notifier = Notifier(self.trades_db, self.settings_db)
        self.exchange = BybitExchange(
            config.DEPO_USDT, 
            self.trades_db, 
            self.settings_db, 
            self.coins_db, 
            self.notifier
        )
        self.notifier.set_exchange(self.exchange)
        
        # Настройка веб-контекста
        set_context(self.db, self.exchange, self.trades_db, self.settings_db, self.coins_db)

        # Telegram клиент
        self.client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
        self.notifier.set_tg_client(self.client)
        
        # Очередь для асинхронной обработки сигналов без блокировки хендлера Telegram
        self.signal_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

    async def _telegram_handler(self, event: events.NewMessage.Event) -> None:
        """
        Обработчик новых сообщений из Telegram. 
        Работает как Producer: быстро принимает сообщение и кладет в очередь.
        """
        if not event.message.message:
            return
        
        time_now = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%H:%M:%S')
        bot_logger.info(f"🔔 [{time_now}] Получено сообщение TG (ID: {event.message.id})")

        try:
            parsed_data: Optional[Dict[str, Any]] = parse_signal(event.message.message)
            if not parsed_data:
                return
            
            parsed_data['received_at'] = datetime.now(timezone.utc).isoformat()
            parsed_data['raw_text'] = event.message.message
            
            # Кладем в очередь и моментально освобождаем хендлер
            await self.signal_queue.put(parsed_data)
            
        except Exception as e:
            bot_logger.error(f"Ошибка при парсинге сообщения в _telegram_handler: {e}")

    async def _signal_worker(self) -> None:
        """
        Фоновый воркер (Consumer), который разгребает очередь сигналов.
        Выполняет обращения к БД и бирже по очереди.
        """
        while True:
            signal = await self.signal_queue.get()
            try:
                # Нативное асинхронное сохранение в БД (aiosqlite)
                is_saved = await self.db.save_signal(signal)
                
                if is_saved:
                    bot_logger.info(f"🚀 Выполнение сигнала для {signal.get('coin')}")
                    await self.exchange.execute_signal(
                        signal['coin'], 
                        signal['signal_type'], 
                        signal['price'], 
                        signal['target_price']
                    )
            except Exception as e:
                bot_logger.error(f"Ошибка при обработке сигнала воркером: {e}")
            finally:
                self.signal_queue.task_done()

    async def run(self) -> None:
        """
        Запуск всех подсистем бота.
        """
        bot_logger.info("Инициализация баз данных...")
        # Инициализируем таблицы и дефолтные значения асинхронно
        await self.db.init_db()
        await self.trades_db.init_db()
        await self.settings_db.init_db()
        await self.coins_db.init_db()

        # Оборачиваем вызов загрузки позиций в поток (пока мы не сделали bybit_exchange асинхронным)
        await asyncio.to_thread(self.exchange.load_active_positions)
        
        # Регистрируем хендлер
        self.client.add_event_handler(
            self._telegram_handler, 
            events.NewMessage(chats=config.TARGET_CHANNEL)
        )

        uvicorn_config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
        server = uvicorn.Server(uvicorn_config)
        
        bot_logger.info("🚀 Запуск всех систем бота...")
        
        try:
            # Запускаем все фоновые задачи параллельно
            await asyncio.gather(
                server.serve(), 
                self.client.start(), 
                self.exchange.monitor_fills(),
                self.notifier.start_polling(),
                self._signal_worker() # Запуск фонового воркера очереди
            )
        except asyncio.CancelledError:
            bot_logger.info("Задачи были отменены (остановка работы).")
        except Exception as e:
            bot_logger.error(f"Критическая ошибка в main loop: {e}")
        finally:
            bot_logger.info("📴 Системы останавливаются...")
            await self.client.disconnect()
            bot_logger.info("🛑 РАБОТА ЗАВЕРШЕНА: Бот полностью остановлен.")


def main_sync() -> None:
    """Синхронная точка входа для настройки политик и запуска цикла."""
    if sys.platform == "win32": 
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    bot = TradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nЗавершение программы пользователем (Ctrl+C).")


if __name__ == '__main__':
    main_sync()