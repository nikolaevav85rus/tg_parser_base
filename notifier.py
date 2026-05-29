import asyncio
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, Optional
import aiohttp

import config
from database import TradesDatabase, SettingsDatabase
from logger import bot_logger

if TYPE_CHECKING:
    from bybit_exchange import BybitExchange
    from telethon import TelegramClient


class Notifier:
    """Сервис для отправки уведомлений и обработки команд из Telegram."""
    
    def __init__(self, trades_db: TradesDatabase, settings_db: SettingsDatabase) -> None:
        self.t_db = trades_db
        self.s_db = settings_db
        self.token = config.TG_NOTIFIER_TOKEN
        self.users = config.ALLOWED_USERS
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        
        self.exchange_ref: Optional["BybitExchange"] = None
        self.tg_client: Optional["TelegramClient"] = None
        self._commands: Dict[str, Callable[[int], Coroutine]] = {}

    def set_exchange(self, ex: "BybitExchange") -> None:
        self.exchange_ref = ex
        self._commands = {
            "/status":  self._cmd_status,
            "/balance": self._cmd_balance,
            "/stop":    self._cmd_stop,
            "/go":      self._cmd_go,
        }

    def set_tg_client(self, client: "TelegramClient") -> None:
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
                    
                await asyncio.sleep(config.NOTIFIER_POLL_INTERVAL)

    async def process_cmd(self, uid: int, text: str) -> None:
        cmd = text.strip().lower()
        if cmd in ("/start", "/help", "help", "старт"):
            await self._cmd_help(uid)
            return
        handler = getattr(self, '_commands', {}).get(cmd)
        if handler:
            await handler(uid)
        else:
            await self.reply_to_user(uid, "❓ Неизвестная команда. Жми /help")

    async def _cmd_help(self, uid: int) -> None:
        await self.reply_to_user(
            uid,
            "🤖 <b>Меню управления:</b>\n\n"
            "/status — Статус систем и активные сделки\n"
            "/balance — Баланс и PNL\n"
            "/stop — Запретить новые входы\n"
            "/go — Разрешить новые входы"
        )

    @staticmethod
    def _fmt_msk(iso_s: Optional[str]) -> str:
        """ISO-строка времени (UTC) → строка в МСК формата ДД.ММ.ГГГГ ЧЧ:ММ."""
        if not iso_s:
            return "—"
        try:
            dt = datetime.fromisoformat(str(iso_s).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return str(iso_s)

    async def _cmd_status(self, uid: int) -> None:
        bybit_ok = await self.exchange_ref.check_connection() if self.exchange_ref else False
        tg_ok = self.tg_client.is_connected() if self.tg_client else False

        bybit_status = "🟢 Подключено" if bybit_ok else "🔴 Ошибка (проверь сеть/API)"
        tg_status = "🟢 Подключено" if tg_ok else "🔴 Отключено"

        eq = await self.exchange_ref.get_real_equity() if self.exchange_ref else 0.0
        closed = await self.t_db.get_closed_trades()
        pnl = sum((t['net_pnl'] or 0.0) for t in closed)

        msg = (f"📡 <b>Статус систем:</b>\nБиржа (Bybit): {bybit_status}\nТГ-Парсер: {tg_status}\n\n"
               f"💰 <b>Баланс (Bybit):</b> ${eq:.2f}\n"
               f"📈 <b>Зафиксированный PNL:</b> ${pnl:.2f}\n\n")

        open_trades = await self.t_db.get_open_trades()
        if not open_trades:
            msg += "🟢 Нет открытых позиций."
            return await self.reply_to_user(uid, msg)

        # Свежая live-статистика с биржи (unrealisedPnl) для расчёта текущего состояния
        if self.exchange_ref:
            try:
                await self.exchange_ref.fetch_live_stats()
            except Exception as e:
                bot_logger.warning(f"Не удалось обновить live_stats для /status: {e}")

        msg += "📊 <b>Ваши открытые позиции:</b>\n\n"
        for i, t in enumerate(open_trades, 1):
            coin = t['coin']
            inv = t['total_inv'] or 0.0
            avg = t['avg_p'] or 0.0
            qty = (inv / avg) if avg > 0 else 0.0
            opened = self._fmt_msk(t['created_at'])

            live = self.exchange_ref.live_stats.get(coin, {}) if self.exchange_ref else {}
            pnl_usd = float(live.get('unrealisedPnl', 0.0))
            pnl_pct = (pnl_usd / inv * 100) if inv > 0 else 0.0
            emoji = "🔴" if pnl_usd < 0 else "🟢"

            msg += (
                f"{i}. <b>{coin}</b>\n"
                f"   💰 Маржа: {inv:.2f} USDT\n"
                f"   📈 Цена входа: {avg:.8f}\n"
                f"   📊 Количество: {qty:.8f}\n"
                f"   📅 Открыта: {opened} МСК\n"
                f"   {emoji} Текущее состояние (≈): {pnl_pct:+.2f}% ({pnl_usd:+.2f} USDT)\n\n"
            )
        await self.reply_to_user(uid, msg)

    async def _cmd_balance(self, uid: int) -> None:
        eq = await self.exchange_ref.get_real_equity() if self.exchange_ref else 0.0
        closed = await self.t_db.get_closed_trades()
        pnl = sum((t['net_pnl'] or 0.0) for t in closed)
        await self.reply_to_user(
            uid,
            f"💰 <b>Баланс (Bybit):</b> ${eq:.2f}\n"
            f"📈 <b>Зафиксированный PNL:</b> ${pnl:.2f}"
        )

    async def _cmd_stop(self, uid: int) -> None:
        await self.s_db.set("allow_open", "False")
        bot_logger.info(f"🚫 Пользователь {uid} отключил новые входы через TG.")
        await self.send("🛑 <b>Новые входы ЗАПРЕЩЕНЫ.</b> Бот будет только усреднять и закрывать.")

    async def _cmd_go(self, uid: int) -> None:
        await self.s_db.set("allow_open", "True")
        bot_logger.info(f"✅ Пользователь {uid} разрешил новые входы через TG.")
        await self.send("✅ <b>Новые входы РАЗРЕШЕНЫ.</b>")