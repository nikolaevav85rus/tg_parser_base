import asyncio
from typing import Any, Optional
import aiohttp

import config
from database import TradesDatabase, SettingsDatabase
from logger import bot_logger


class Notifier:
    """Сервис для отправки уведомлений и обработки команд из Telegram."""
    
    def __init__(self, trades_db: TradesDatabase, settings_db: SettingsDatabase) -> None:
        self.t_db = trades_db
        self.s_db = settings_db
        self.token = config.TG_NOTIFIER_TOKEN
        self.users = config.ALLOWED_USERS
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        
        # Ссылки на другие подсистемы
        self.exchange_ref: Optional[Any] = None 
        self.tg_client: Optional[Any] = None

    def set_exchange(self, ex: Any) -> None:
        """Инъекция зависимости: клиент биржи."""
        self.exchange_ref = ex
        
    def set_tg_client(self, client: Any) -> None:
        """Инъекция зависимости: клиент Telethon (парсер)."""
        self.tg_client = client

    async def send(self, text: str) -> None:
        """Массовая отправка уведомления всем разрешенным пользователям."""
        if not self.token or not self.users: 
            return
            
        async with aiohttp.ClientSession() as session:
            for uid in self.users:
                try:
                    await session.post(
                        f"{self.api_url}/sendMessage", 
                        json={
                            "chat_id": uid, 
                            "text": text, 
                            "parse_mode": "HTML", 
                            "disable_web_page_preview": True
                        }
                    )
                except Exception as e:
                    bot_logger.error(f"⚠️ Ошибка отправки уведомления для {uid}: {e}")

    async def reply_to_user(self, uid: int, text: str) -> None:
        """Отправка ответа конкретному пользователю."""
        if not self.token: 
            return
            
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(
                    f"{self.api_url}/sendMessage", 
                    json={
                        "chat_id": uid, 
                        "text": text, 
                        "parse_mode": "HTML", 
                        "disable_web_page_preview": True
                    }
                )
            except Exception as e:
                bot_logger.error(f"⚠️ Ошибка отправки ответа пользователю {uid}: {e}")

    async def start_polling(self) -> None:
        """Фоновый цикл опроса серверов Telegram (Long Polling) для приема команд."""
        if not self.token or not self.users:
            bot_logger.warning("⚠️ Токен бота-уведомителя не настроен. Оповещения и команды Telegram отключены.")
            return
            
        bot_logger.info("✅ Бот-уведомитель успешно запущен и готов к приему команд.")
        
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(f"{self.api_url}/getUpdates", params={"offset": self.offset, "timeout": 10}) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            for req in data["result"]:
                                self.offset = req["update_id"] + 1
                                msg = req.get("message")
                                
                                if msg and "text" in msg:
                                    uid = msg.get("from", {}).get("id")
                                    if uid in self.users:
                                        bot_logger.info(f"📥 Получена команда от пользователя {uid}: {msg['text']}")
                                        await self.process_cmd(uid, msg["text"])
                except Exception as e:
                    bot_logger.debug(f"Ошибка при опросе обновлений бота: {e}")
                    
                await asyncio.sleep(2)

    async def process_cmd(self, uid: int, text: str) -> None:
        """Асинхронная обработка входящих текстовых команд."""
        cmd = text.strip().lower()
        
        if cmd in ["/start", "/help", "help", "старт"]:
            await self.reply_to_user(
                uid, 
                "🤖 <b>Меню управления:</b>\n\n"
                "/status — Статус систем и активные сделки\n"
                "/balance — Баланс и PNL\n"
                "/stop — Запретить новые входы\n"
                "/go — Разрешить новые входы"
            )
            
        elif cmd == "/status":
            # Вызовы биржи теперь асинхронные
            bybit_ok = await self.exchange_ref.check_connection() if self.exchange_ref else False
            tg_ok = self.tg_client.is_connected() if self.tg_client else False
            
            bybit_status = "🟢 Подключено" if bybit_ok else "🔴 Ошибка (проверь сеть/API)"
            tg_status = "🟢 Подключено" if tg_ok else "🔴 Отключено"
            
            msg = f"📡 <b>Статус систем:</b>\nБиржа (Bybit): {bybit_status}\nТГ-Парсер: {tg_status}\n\n"
            
            # Вызов БД теперь асинхронный
            open_trades = await self.t_db.get_open_trades()
            if not open_trades: 
                msg += "🟢 Нет открытых позиций."
                return await self.reply_to_user(uid, msg)
            
            msg += "📊 <b>Активные позиции:</b>\n\n"
            for t in open_trades:
                # Используем безопасный доступ по ключам благодаря aiosqlite.Row
                msg += (f"🔸 <b>{t['coin']}</b> (Шаг {t['step']})\n"
                        f"Вложено: ${t['total_inv']:.2f} | Ср.цена: {t['avg_p']:.4f}\n\n")
            await self.reply_to_user(uid, msg)
            
        elif cmd == "/balance":
            # Вызовы асинхронные
            eq = await self.exchange_ref.get_real_equity() if self.exchange_ref else 0.0
            closed = await self.t_db.get_closed_trades()
            
            # Считаем чистый абсолютный профит (net_pnl), а не процентный
            pnl = sum((t['net_pnl'] or 0.0) for t in closed) 
            
            await self.reply_to_user(
                uid, 
                f"💰 <b>Баланс (Bybit):</b> ${eq:.2f}\n"
                f"📈 <b>Зафиксированный PNL:</b> ${pnl:.2f}"
            )
            
        elif cmd == "/stop":
            # Установка настроек теперь асинхронная
            await self.s_db.set("allow_open", "False")
            bot_logger.info(f"🚫 Пользователь {uid} отключил новые входы через TG.")
            await self.send("🛑 <b>Новые входы ЗАПРЕЩЕНЫ.</b> Бот будет только усреднять и закрывать.")
            
        elif cmd == "/go":
            await self.s_db.set("allow_open", "True")
            bot_logger.info(f"✅ Пользователь {uid} разрешил новые входы через TG.")
            await self.send("✅ <b>Новые входы РАЗРЕШЕНЫ.</b>")
            
        else:
            await self.reply_to_user(uid, "❓ Неизвестная команда. Жми /help")