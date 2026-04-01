import math, asyncio
from pybit.unified_trading import HTTP
import config
from database import TradesDatabase, SettingsDatabase, CoinsDatabase
from logger import bot_logger

class BybitExchange:
    def __init__(self, initial_limit: float, trades_db: TradesDatabase, settings_db: SettingsDatabase, coins_db: CoinsDatabase, notifier):
        self.db = trades_db
        self.settings = settings_db
        self.coins_db = coins_db
        self.notifier = notifier
        self.session = HTTP(testnet=config.BYBIT_TESTNET, api_key=config.BYBIT_API_KEY, api_secret=config.BYBIT_API_SECRET)
        
        saved_limit = self.settings.get("trade_limit")
        self.trade_limit = float(saved_limit) if saved_limit else initial_limit
        self.active_positions = {}
        self.instrument_info_cache = {}
        self.price_step_cache = {}
        self.leverage_cache = {} # Кэш для предотвращения лишних запросов к API
        self.live_stats = {} 
        bot_logger.info("Биржевой модуль BybitExchange инициализирован.")

    def _get_dca_grid(self):
        """Загрузка динамических настроек сетки из БД."""
        return {
            "tp_target": float(self.settings.get("tp_target", "0.7")),
            "volumes": [
                float(self.settings.get("dca_0", "2.0")),
                float(self.settings.get("dca_1", "4.0")),
                float(self.settings.get("dca_2", "8.0")),
                float(self.settings.get("dca_3", "16.0"))
            ],
            "levels": [
                float(self.settings.get("dca_level_1", "3.5")),
                float(self.settings.get("dca_level_2", "6.5")),
                float(self.settings.get("dca_level_3", "14.5"))
            ]
        }

    async def _ensure_leverage(self, symbol, target_leverage):
        """
        Проверяет текущее плечо и устанавливает его только при необходимости.
        Исключает ошибку 110043 (leverage not modified).
        """
        if self.leverage_cache.get(symbol) == target_leverage:
            return True

        try:
            # Проверяем реальное состояние на бирже
            res = await asyncio.to_thread(self.session.get_positions, category="linear", symbol=symbol)
            if res['retCode'] == 0 and res['result']['list']:
                current_lev = int(res['result']['list'][0]['leverage'])
                if current_lev == target_leverage:
                    self.leverage_cache[symbol] = target_leverage
                    return True

            # Устанавливаем только если отличается
            await asyncio.to_thread(
                self.session.set_leverage,
                category="linear", symbol=symbol,
                buyLeverage=str(target_leverage),
                sellLeverage=str(target_leverage)
            )
            self.leverage_cache[symbol] = target_leverage
            bot_logger.info(f"Плечо {target_leverage}x установлено для {symbol}")
            return True
        except Exception as e:
            if "110043" in str(e):
                self.leverage_cache[symbol] = target_leverage
                return True
            bot_logger.warning(f"Не удалось проверить/установить плечо для {symbol}: {e}")
            # Возвращаем True, так как ошибка установки плеча не должна блокировать вход, 
            # если оно уже могло быть установлено ранее вручную.
            return True

    def check_connection(self):
        try: 
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            return res.get('retCode') == 0
        except: return False

    def update_limit(self, new_limit: float):
        self.trade_limit = new_limit
        self.settings.set("trade_limit", new_limit)
        bot_logger.info(f"Лимит на сделку обновлен: ${new_limit}")

    def get_real_equity(self):
        try:
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if res['retCode'] == 0: 
                return float(res['result']['list'][0]['coin'][0]['equity'])
        except: pass
        return 0.0

    def load_active_positions(self):
        raw = self.db.get_open_trades()
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

    async def fetch_live_stats(self):
        try:
            res = await asyncio.to_thread(self.session.get_positions, category="linear", settleCoin="USDT")
            if res['retCode'] == 0:
                self.live_stats = {p['symbol']: {"unrealisedPnl": float(p['unrealisedPnl'])} for p in res['result']['list'] if float(p['size']) > 0}
        except: pass

    async def _get_qty_step(self, symbol):
        if symbol in self.instrument_info_cache: return self.instrument_info_cache[symbol]
        try:
            res = await asyncio.to_thread(self.session.get_instruments_info, category="linear", symbol=symbol)
            step = float(res['result']['list'][0]['lotSizeFilter']['qtyStep'])
            self.instrument_info_cache[symbol] = step
            return step
        except: return 0.001 

    async def _get_price_step(self, symbol):
        if symbol in self.price_step_cache: return self.price_step_cache[symbol]
        try:
            res = await asyncio.to_thread(self.session.get_instruments_info, category="linear", symbol=symbol)
            step = float(res['result']['list'][0]['priceFilter']['tickSize'])
            self.price_step_cache[symbol] = step
            return step
        except: return 0.01

    def _round_qty(self, qty, qty_step):
        precision = int(abs(math.log10(qty_step))) if qty_step < 1 else 0
        return round(math.floor(qty / qty_step) * qty_step, precision)

    def _round_price(self, price, price_step):
        precision = int(abs(math.log10(price_step))) if price_step < 1 else 0
        return round(math.floor(price / price_step) * price_step, precision)

    def _calc_qty(self, deposit: float, percent: float, leverage: int, price: float, qty_step: float):
        amount_usdt = deposit * (percent / 100.0)
        qty = (amount_usdt * leverage) / price
        return self._round_qty(qty, qty_step)

    async def _place_limit(self, symbol: str, side: str, qty: float, price: float, reduce_only: bool = False):
        return await asyncio.to_thread(
            self.session.place_order,
            category="linear", symbol=symbol, side=side, orderType="Limit",
            qty=str(qty), price=str(price), timeInForce="GTC", reduceOnly=reduce_only
        )

    async def _cancel_order_safe(self, symbol: str, order_id: str | None):
        if not order_id: return
        try: await asyncio.to_thread(self.session.cancel_order, category="linear", symbol=symbol, orderId=order_id)
        except: pass

    async def _get_open_order_ids(self, symbol: str):
        try:
            res = await asyncio.to_thread(self.session.get_open_orders, category="linear", symbol=symbol)
            if res.get("retCode") == 0:
                return {o.get("orderId") for o in res["result"]["list"]}
        except: return set()

    async def _get_real_execution_data(self, order_id, symbol):
        await asyncio.sleep(1.2) 
        try:
            res = await asyncio.to_thread(self.session.get_executions, category="linear", symbol=symbol, orderId=order_id)
            if res['retCode'] == 0 and res['result']['list']:
                ex = res['result']['list']
                q = sum(float(e['execQty']) for e in ex)
                v = sum(float(e['execValue']) for e in ex)
                f = sum(float(e['execFee']) for e in ex)
                if q > 0: return v / q, v, f
        except: pass
        return None, None, 0.0

    async def execute_signal(self, coin, signal_type, signal_price, target_price):
        """Вход в сделку."""
        bot_logger.info(f"⚡ ТОРГОВЫЙ МОДУЛЬ: Получена команда {signal_type} для {coin}")
        
        coin_info = self.coins_db.get_coin(coin)
        if not coin_info or not coin_info["is_active"]: return 
        if coin_info["alias"]: coin = coin_info["alias"]

        if self.db.get_trading_trade(coin):
            bot_logger.warning(f"Пропуск {signal_type} для {coin}: активная сделка уже существует.")
            return

        if signal_type == "OPEN":
            grid = self._get_dca_grid()
            leverage = int(self.settings.get("leverage", config.LEVERAGE))
            deposit = float(self.settings.get("trade_limit", self.trade_limit))
            
            # Установка плеча только при необходимости (идемпотентно)
            await self._ensure_leverage(coin, leverage)
            
            try:
                tick = await asyncio.to_thread(self.session.get_tickers, category="linear", symbol=coin)
                price = float(tick['result']['list'][0]['lastPrice'])
                
                qty_step = await self._get_qty_step(coin)
                price_step = await self._get_price_step(coin)
                
                qty = self._calc_qty(deposit, grid["volumes"][0], leverage, price, qty_step)
                if qty <= 0: return

                order = await asyncio.to_thread(self.session.place_order, category="linear", symbol=coin, side="Buy", orderType="Market", qty=str(qty))
                
                if order.get('retCode') == 0:
                    real_p, real_inv, exec_fee = await self._get_real_execution_data(order['result']['orderId'], coin)
                    real_p, real_inv = real_p or price, real_inv or (qty * price)
                    
                    self.db.create_trade(coin, real_p, real_inv, 0.0, leverage, exec_fee)
                    bot_logger.info(f"Исполнен вход {coin}. Цена: {real_p}.")

                    # 1. Лимитный Тейк-Профит
                    tp_price = self._round_price(real_p * (1 + grid["tp_target"] / 100.0), price_step)
                    tp_order = await self._place_limit(coin, "Sell", qty, tp_price, reduce_only=True)
                    if tp_order.get("retCode") == 0:
                        self.db.set_tp_order_id(coin, tp_order["result"]["orderId"])

                    # 2. Лимитный DCA_1 (от цены входа)
                    dca1_price = self._round_price(real_p * (1 - grid["levels"][0] / 100.0), price_step)
                    dca1_qty = self._calc_qty(deposit, grid["volumes"][1], leverage, dca1_price, qty_step)
                    dca_order = await self._place_limit(coin, "Buy", dca1_qty, dca1_price)
                    if dca_order.get("retCode") == 0:
                        self.db.set_dca_order_id(coin, dca_order["result"]["orderId"])

                    await self.notifier.send(f"🟢 <b>ВХОД: {coin}</b>\nЦена: {real_p}\nTP: {tp_price}\nDCA_1: {dca1_price}")
                    self.load_active_positions()
            except Exception as e:
                bot_logger.critical(f"Ошибка OPEN {coin}: {e}")

    async def monitor_fills(self):
        """Мониторинг исполнения сетки (DCA и TP). Плечо здесь НЕ меняется."""
        while True:
            await asyncio.sleep(2.0)
            try:
                open_trades = self.db.get_open_trades()
                for trade in open_trades:
                    coin = trade["coin"]
                    open_order_ids = await self._get_open_order_ids(coin)

                    # 1. Проверка TP
                    if trade["tp_order_id"] and trade["tp_order_id"] not in open_order_ids:
                        res_p, _, fee = await self._get_real_execution_data(trade["tp_order_id"], coin)
                        if res_p:
                            _, _, net_pnl = self.db.close_trade(coin, res_p, fee)
                            await self._cancel_order_safe(coin, trade["dca_order_id"])
                            bot_logger.info(f"✅ TP Исполнен {coin}. PNL: ${net_pnl:.2f}")
                            await self.notifier.send(f"✅ <b>TP: {coin}</b>\nЦена: {res_p}\nNet PNL: <b>{net_pnl:.2f}$</b>")
                            self.load_active_positions()
                            continue

                    # 2. Проверка DCA (Recursive)
                    if trade["dca_order_id"] and trade["dca_order_id"] not in open_order_ids:
                        filled_p, filled_inv, fee = await self._get_real_execution_data(trade["dca_order_id"], coin)
                        if not filled_p: continue
                        
                        next_step = int(trade["step"]) + 1
                        self.db.update_trade_dca(coin, next_step, filled_p, filled_inv, fee)
                        
                        updated = self.db.get_trading_trade(coin)
                        await self._cancel_order_safe(coin, trade["tp_order_id"])
                        
                        qty_step = await self._get_qty_step(coin)
                        price_step = await self._get_price_step(coin)
                        grid = self._get_dca_grid()
                        leverage = int(trade["leverage"])
                        deposit = float(self.settings.get("trade_limit", self.trade_limit))
                        
                        total_qty = self._round_qty(updated["total_inv"] / updated["avg_p"], qty_step)
                        
                        # Новый TP от новой средней цены
                        new_tp = self._round_price(updated["avg_p"] * (1 + grid["tp_target"] / 100.0), price_step)
                        tp_res = await self._place_limit(coin, "Sell", total_qty, new_tp, reduce_only=True)
                        if tp_res.get("retCode") == 0:
                            self.db.set_tp_order_id(coin, tp_res["result"]["orderId"])
                        
                        # Новый DCA от цены последнего исполнения
                        if next_step < 3:
                            next_dev = grid["levels"][next_step]
                            next_vol = grid["volumes"][next_step + 1]
                            next_p = self._round_price(filled_p * (1 - next_dev / 100.0), price_step)
                            next_q = self._calc_qty(deposit, next_vol, leverage, next_p, qty_step)
                            
                            dca_res = await self._place_limit(coin, "Buy", next_q, next_p)
                            if dca_res.get("retCode") == 0:
                                self.db.set_dca_order_id(coin, dca_res["result"]["orderId"])
                        
                        await self.notifier.send(f"⚖️ <b>DCA #{next_step}: {coin}</b>\nСр.цена: {updated['avg_p']:.4f}\nНовый TP: {new_tp}")
                        self.load_active_positions()

            except Exception as e:
                bot_logger.error(f"Ошибка мониторинга: {e}")