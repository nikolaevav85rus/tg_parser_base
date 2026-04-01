"""
Модуль для взаимодействия с API биржи Bybit (Unified Trading Account).
Обеспечивает выставление ордеров, управление позициями, расчет сетки DCA 
и обработку торговых сигналов.
"""

import math
import asyncio
from typing import Dict, Any, Tuple, Optional, Set

from pybit.unified_trading import HTTP

import config
from database import TradesDatabase, SettingsDatabase, CoinsDatabase
from logger import bot_logger


class BybitExchange:
    """
    Основной класс для работы с биржей Bybit.
    Управляет торговым циклом, кэшированием данных и мониторингом ордеров.
    """
    
    def __init__(self, initial_limit: float, trades_db: TradesDatabase, settings_db: SettingsDatabase, coins_db: CoinsDatabase, notifier: Any) -> None:
        """
        Инициализация биржевого клиента и локальных кэшей.
        
        :param initial_limit: Базовый лимит депозита на одну сделку (в USDT).
        :param trades_db: Объект базы данных активных и закрытых сделок.
        :param settings_db: Объект базы данных настроек торговой стратегии.
        :param coins_db: Объект базы данных разрешенных монет (Whitelist).
        :param notifier: Объект для отправки уведомлений.
        """
        self.db = trades_db
        self.settings = settings_db
        self.coins_db = coins_db
        self.notifier = notifier
        
        # Инициализация HTTP клиента Bybit v5
        self.session = HTTP(
            testnet=config.BYBIT_TESTNET, 
            api_key=config.BYBIT_API_KEY, 
            api_secret=config.BYBIT_API_SECRET
        )
        
        self.trade_limit = initial_limit
        
        # Словари для хранения данных в памяти (кэширование)
        self.active_positions: Dict[str, Dict[str, Any]] = {}       
        self.instrument_info_cache: Dict[str, float] = {}  
        self.price_step_cache: Dict[str, float] = {}       
        self.leverage_cache: Dict[str, int] = {}         
        self.live_stats: Dict[str, Dict[str, float]] = {}             
        
        bot_logger.info("Биржевой модуль BybitExchange инициализирован.")

    async def _init_settings(self) -> None:
        """Асинхронная загрузка стартовых настроек из БД (вызывается извне)."""
        saved_limit = await self.settings.get("trade_limit")
        if saved_limit:
            self.trade_limit = float(saved_limit)

    async def _get_dca_grid(self) -> Dict[str, Any]:
        """
        Загрузка динамических настроек сетки DCA и Take Profit из базы данных.
        
        :return: Словарь с целевым профитом, объемами для шагов и уровнями отклонений.
        """
        return {
            "tp_target": float(await self.settings.get("tp_target", "0.7")),
            "volumes": [
                float(await self.settings.get("dca_0", "2.0")),
                float(await self.settings.get("dca_1", "4.0")),
                float(await self.settings.get("dca_2", "8.0")),
                float(await self.settings.get("dca_3", "16.0"))
            ],
            "levels": [
                float(await self.settings.get("dca_level_1", "3.5")),
                float(await self.settings.get("dca_level_2", "6.5")),
                float(await self.settings.get("dca_level_3", "14.5"))
            ]
        }

    async def _api_call(self, func: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Централизованная обертка для асинхронного вызова синхронных методов pybit.
        Обеспечивает перехват сетевых ошибок и стандартизированное логирование.
        """
        try:
            res = await asyncio.to_thread(func, *args, **kwargs)
            if isinstance(res, dict) and res.get('retCode') != 0:
                bot_logger.error(f"API Ошибка: {res.get('retMsg')} (Код: {res.get('retCode')})")
            return res # type: ignore
        except Exception as e:
            bot_logger.error(f"Сетевая/Внутренняя ошибка API: {e}")
            return {'retCode': -1, 'retMsg': str(e)}

    async def _ensure_leverage(self, symbol: str, target_leverage: int) -> bool:
        """
        Проверка текущего кредитного плеча и его установка только при необходимости.
        Идемпотентная операция: предотвращает ошибку 110043 (leverage not modified).
        """
        if self.leverage_cache.get(symbol) == target_leverage:
            return True

        res = await self._api_call(self.session.get_positions, category="linear", symbol=symbol)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            current_lev = int(res['result']['list'][0]['leverage'])
            if current_lev == target_leverage:
                self.leverage_cache[symbol] = target_leverage
                return True

        res = await self._api_call(
            self.session.set_leverage,
            category="linear", symbol=symbol,
            buyLeverage=str(target_leverage),
            sellLeverage=str(target_leverage)
        )
        
        if res.get('retCode') == 0 or "110043" in str(res.get('retMsg', '')):
            self.leverage_cache[symbol] = target_leverage
            bot_logger.info(f"Плечо {target_leverage}x подтверждено для {symbol}")
            return True
            
        bot_logger.warning(f"Не удалось проверить/установить плечо для {symbol}")
        return True 

    async def check_connection(self) -> bool:
        """Проверка связи с API Bybit путем запроса баланса USDT."""
        res = await self._api_call(self.session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        return res.get('retCode') == 0

    async def update_limit(self, new_limit: float) -> None:
        """Обновление лимита депозита в оперативной памяти и БД."""
        self.trade_limit = new_limit
        await self.settings.set("trade_limit", new_limit)
        bot_logger.info(f"Лимит на сделку обновлен: ${new_limit}")

    async def get_real_equity(self) -> float:
        """Получение реального баланса аккаунта (Equity) с учетом плавающего PNL."""
        res = await self._api_call(self.session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        if res.get('retCode') == 0: 
            return float(res['result']['list'][0]['coin'][0]['equity'])
        return 0.0

    async def load_active_positions(self) -> None:
        """Синхронизация локального кэша активных сделок с базой данных."""
        raw = await self.db.get_open_trades()
        self.active_positions = {
            t['coin']: {
                "step": t['step'], 
                "invested": t['total_inv'], 
                "avg_price": t['avg_p'], 
                "target_price": t['target_p'] or 0.0, 
                "open_fee": t['open_fee']
            } for t in raw
        }
        bot_logger.info(f"Загружено активных позиций из БД: {len(self.active_positions)}")

    async def fetch_live_stats(self) -> None:
        """Асинхронный опрос биржи для получения нереализованного PNL."""
        res = await self._api_call(self.session.get_positions, category="linear", settleCoin="USDT")
        if res.get('retCode') == 0:
            self.live_stats = {
                p['symbol']: {"unrealisedPnl": float(p['unrealisedPnl'])} 
                for p in res['result']['list'] if float(p['size']) > 0
            }

    async def _get_instrument_info(self, symbol: str) -> Tuple[float, float]:
        """Получение параметров инструмента (минимальный шаг объема и цены)."""
        if symbol not in self.instrument_info_cache or symbol not in self.price_step_cache:
            res = await self._api_call(self.session.get_instruments_info, category="linear", symbol=symbol)
            if res.get('retCode') == 0 and res.get('result', {}).get('list'):
                info = res['result']['list'][0]
                self.instrument_info_cache[symbol] = float(info['lotSizeFilter']['qtyStep'])
                self.price_step_cache[symbol] = float(info['priceFilter']['tickSize'])
            else:
                return 0.001, 0.01
        return self.instrument_info_cache[symbol], self.price_step_cache[symbol]

    def _round_value(self, value: float, step: float) -> float:
        """Математическая функция округления цены или объема до требуемой точности."""
        precision = int(abs(math.log10(step))) if step < 1 else 0
        return round(math.floor(value / step) * step, precision)

    def _calc_qty(self, deposit: float, percent: float, leverage: int, price: float, qty_step: float) -> float:
        """Расчет объема ордера в монетах."""
        amount_usdt = deposit * (percent / 100.0)
        qty = (amount_usdt * leverage) / price
        return self._round_value(qty, qty_step)

    async def _place_limit(self, symbol: str, side: str, qty: float, price: float, reduce_only: bool = False) -> Dict[str, Any]:
        """Отправка лимитного ордера (GTC)."""
        return await self._api_call(
            self.session.place_order,
            category="linear", symbol=symbol, side=side, orderType="Limit",
            qty=str(qty), price=str(price), timeInForce="GTC", reduceOnly=reduce_only
        )

    async def _cancel_order_safe(self, symbol: str, order_id: Optional[str]) -> None:
        """Безопасная отмена ордера по его ID."""
        if order_id:
            await self._api_call(self.session.cancel_order, category="linear", symbol=symbol, orderId=order_id)

    async def _get_open_order_ids(self, symbol: str) -> Set[str]:
        """Получение множества ID всех открытых ордеров по монете."""
        res = await self._api_call(self.session.get_open_orders, category="linear", symbol=symbol)
        if res.get("retCode") == 0:
            return {o.get("orderId") for o in res["result"]["list"]}
        return set()

    async def _get_real_execution_data(self, order_id: str, symbol: str) -> Tuple[Optional[float], Optional[float], float]:
        """Получение фактических данных об исполнении ордера."""
        await asyncio.sleep(1.2)
        res = await self._api_call(self.session.get_executions, category="linear", symbol=symbol, orderId=order_id)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            ex = res['result']['list']
            q = sum(float(e['execQty']) for e in ex)
            v = sum(float(e['execValue']) for e in ex)
            f = sum(float(e['execFee']) for e in ex)
            if q > 0: 
                return v / q, v, f
        return None, None, 0.0

    async def execute_signal(self, coin: str, signal_type: str, signal_price: float, target_price: float) -> None:
        """Внешний интерфейс для приема торговых сигналов."""
        bot_logger.info(f"⚡ ТОРГОВЫЙ МОДУЛЬ: Получена команда {signal_type} для {coin}")
        
        coin_info = await self.coins_db.get_coin(coin)
        if not coin_info or not coin_info["is_active"]: 
            return 
        
        coin_alias = coin_info.get("alias") or coin

        # Защита от дублирования сделок
        if await self.db.get_trading_trade(coin_alias):
            bot_logger.warning(f"Пропуск {signal_type} для {coin_alias}: активная сделка уже существует.")
            return

        if signal_type == "OPEN":
            await self._handle_open_signal(coin_alias)

    async def _handle_open_signal(self, coin: str) -> None:
        """Логика первичного входа в позицию (Market Order)."""
        grid = await self._get_dca_grid()
        leverage = int(await self.settings.get("leverage", str(config.LEVERAGE)))
        deposit = float(await self.settings.get("trade_limit", str(self.trade_limit)))
        
        await self._ensure_leverage(coin, leverage)
        
        try:
            res = await self._api_call(self.session.get_tickers, category="linear", symbol=coin)
            if res.get('retCode') != 0: 
                return
            
            price = float(res['result']['list'][0]['lastPrice'])
            qty_step, price_step = await self._get_instrument_info(coin)
            
            qty = self._calc_qty(deposit, grid["volumes"][0], leverage, price, qty_step)
            if qty <= 0: 
                return

            order = await self._api_call(
                self.session.place_order, 
                category="linear", symbol=coin, side="Buy", orderType="Market", qty=str(qty)
            )
            
            if order.get('retCode') == 0:
                order_id = order['result']['orderId']
                
                real_p, real_inv, exec_fee = await self._get_real_execution_data(order_id, coin)
                real_p = real_p or price
                real_inv = real_inv or (qty * price)
                
                await self.db.create_trade(coin, real_p, real_inv, 0.0, leverage, exec_fee)
                bot_logger.info(f"Исполнен вход {coin}. Цена: {real_p}.")

                tp_price = self._round_value(real_p * (1 + grid["tp_target"] / 100.0), price_step)
                tp_order = await self._place_limit(coin, "Sell", qty, tp_price, reduce_only=True)
                if tp_order.get("retCode") == 0:
                    await self.db.set_tp_order_id(coin, tp_order["result"]["orderId"])

                dca1_price = self._round_value(real_p * (1 - grid["levels"][0] / 100.0), price_step)
                dca1_qty = self._calc_qty(deposit, grid["volumes"][1], leverage, dca1_price, qty_step)
                dca_order = await self._place_limit(coin, "Buy", dca1_qty, dca1_price)
                if dca_order.get("retCode") == 0:
                    await self.db.set_dca_order_id(coin, dca_order["result"]["orderId"])

                await self.notifier.send(f"🟢 <b>ВХОД: {coin}</b>\nЦена: {real_p}\nTP: {tp_price}\nDCA_1: {dca1_price}")
                await self.load_active_positions()
        except Exception as e:
            bot_logger.error(f"Ошибка OPEN {coin}: {e}")

    async def monitor_fills(self) -> None:
        """Бесконечный фоновый цикл (Daemon) для опроса статуса лимитных ордеров."""
        while True:
            await asyncio.sleep(2.0) 
            try:
                open_trades = await self.db.get_open_trades()
                for trade in open_trades:
                    # Защита от спама API Bybit: небольшая задержка перед проверкой каждой монеты
                    await asyncio.sleep(0.1) 
                    await self._process_trade_fills(trade)
            except Exception as e:
                bot_logger.error(f"Ошибка мониторинга: {e}")

    async def _process_trade_fills(self, trade: Any) -> None:
        """Определение, какой именно ордер исполнился по текущей сделке."""
        coin = trade["coin"]
        open_order_ids = await self._get_open_order_ids(coin)

        if trade["tp_order_id"] and trade["tp_order_id"] not in open_order_ids:
            await self._process_tp_execution(trade, coin)
            return

        if trade["dca_order_id"] and trade["dca_order_id"] not in open_order_ids:
            await self._process_dca_execution(trade, coin)

    async def _process_tp_execution(self, trade: Any, coin: str) -> None:
        """Обработка успешного закрытия сделки по Take Profit."""
        res_p, _, fee = await self._get_real_execution_data(trade["tp_order_id"], coin)
        if res_p:
            _, _, net_pnl = await self.db.close_trade(coin, res_p, fee)
            await self._cancel_order_safe(coin, trade["dca_order_id"])
            
            bot_logger.info(f"✅ TP Исполнен {coin}. PNL: ${net_pnl:.2f}")
            await self.notifier.send(f"✅ <b>TP: {coin}</b>\nЦена: {res_p}\nNet PNL: <b>{net_pnl:.2f}$</b>")
            await self.load_active_positions()

    async def _process_dca_execution(self, trade: Any, coin: str) -> None:
        """Обработка усреднения (DCA)."""
        filled_p, filled_inv, fee = await self._get_real_execution_data(trade["dca_order_id"], coin)
        if not filled_p: 
            return
        
        next_step = int(trade["step"]) + 1
        
        # Обновление средней цены (обязательно с await, так как fill_inv нужен для мат. формул)
        # Обрати внимание, что fee может быть 0.0, если биржа не вернула, это не сломает расчет.
        await self.db.update_trade_dca(coin, next_step, filled_p, filled_inv, fee)
        updated = await self.db.get_trading_trade(coin)
        
        if not updated:
            return

        await self._cancel_order_safe(coin, trade["tp_order_id"])
        
        qty_step, price_step = await self._get_instrument_info(coin)
        grid = await self._get_dca_grid()
        leverage = int(trade["leverage"])
        deposit = float(await self.settings.get("trade_limit", str(self.trade_limit)))
        
        total_qty = self._round_value(updated["total_inv"] / updated["avg_p"], qty_step)
        
        new_tp = self._round_value(updated["avg_p"] * (1 + grid["tp_target"] / 100.0), price_step)
        tp_res = await self._place_limit(coin, "Sell", total_qty, new_tp, reduce_only=True)
        if tp_res.get("retCode") == 0:
            await self.db.set_tp_order_id(coin, tp_res["result"]["orderId"])
        
        if next_step < 3: 
            next_dev = grid["levels"][next_step]
            next_vol = grid["volumes"][next_step + 1]
            
            next_p = self._round_value(filled_p * (1 - next_dev / 100.0), price_step)
            next_q = self._calc_qty(deposit, next_vol, leverage, next_p, qty_step)
            
            dca_res = await self._place_limit(coin, "Buy", next_q, next_p)
            if dca_res.get("retCode") == 0:
                await self.db.set_dca_order_id(coin, dca_res["result"]["orderId"])
        
        await self.notifier.send(f"⚖️ <b>DCA #{next_step}: {coin}</b>\nСр.цена: {updated['avg_p']:.4f}\nНовый TP: {new_tp}")
        await self.load_active_positions()