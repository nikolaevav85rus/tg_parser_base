"""
Модуль для взаимодействия с API биржи Bybit (Unified Trading Account).
Обеспечивает выставление ордеров, управление позициями, расчет сетки DCA 
и обработку торговых сигналов.
"""

import math
import asyncio
from pybit.unified_trading import HTTP
import config
from database import TradesDatabase, SettingsDatabase, CoinsDatabase
from logger import bot_logger

class BybitExchange:
    """
    Основной класс для работы с биржей Bybit.
    Управляет торговым циклом, кэшированием данных и мониторингом ордеров.
    """
    
    def __init__(self, initial_limit: float, trades_db: TradesDatabase, settings_db: SettingsDatabase, coins_db: CoinsDatabase, notifier):
        """
        Инициализация биржевого клиента и локальных кэшей.
        
        :param initial_limit: Базовый лимит депозита на одну сделку (в USDT).
        :param trades_db: Объект базы данных активных и закрытых сделок.
        :param settings_db: Объект базы данных настроек торговой стратегии.
        :param coins_db: Объект базы данных разрешенных монет (Whitelist).
        :param notifier: Объект для отправки уведомлений (например, в Telegram).
        """
        self.db = trades_db
        self.settings = settings_db
        self.coins_db = coins_db
        self.notifier = notifier
        
        # Инициализация HTTP клиента Bybit v5
        self.session = HTTP(testnet=config.BYBIT_TESTNET, api_key=config.BYBIT_API_KEY, api_secret=config.BYBIT_API_SECRET)
        
        # Установка торгового лимита из БД или использование переданного по умолчанию
        saved_limit = self.settings.get("trade_limit")
        self.trade_limit = float(saved_limit) if saved_limit else initial_limit
        
        # Словари для хранения данных в памяти (кэширование)
        self.active_positions = {}       # Текущие активные позиции
        self.instrument_info_cache = {}  # Шаги объема (qtyStep) для тикеров
        self.price_step_cache = {}       # Шаги цены (tickSize) для тикеров
        self.leverage_cache = {}         # Текущее установленное плечо для тикеров
        self.live_stats = {}             # Статистика PNL в реальном времени
        
        bot_logger.info("Биржевой модуль BybitExchange инициализирован.")

    def _get_dca_grid(self) -> dict:
        """
        Загрузка динамических настроек сетки DCA и Take Profit из базы данных.
        
        :return: Словарь с целевым профитом, объемами для шагов и уровнями отклонений.
        """
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

    async def _api_call(self, func, *args, **kwargs) -> dict:
        """
        Централизованная обертка для асинхронного вызова синхронных методов pybit.
        Обеспечивает перехват сетевых ошибок и стандартизированное логирование.
        
        :param func: Функция API Bybit для вызова.
        :return: Ответ от биржи в формате словаря.
        """
        try:
            res = await asyncio.to_thread(func, *args, **kwargs)
            # Проверка наличия ошибки на стороне биржи (retCode != 0)
            if isinstance(res, dict) and res.get('retCode') != 0:
                bot_logger.error(f"API Ошибка: {res.get('retMsg')} (Код: {res.get('retCode')})")
            return res
        except Exception as e:
            bot_logger.error(f"Сетевая/Внутренняя ошибка API: {e}")
            return {'retCode': -1, 'retMsg': str(e)}

    async def _ensure_leverage(self, symbol: str, target_leverage: int) -> bool:
        """
        Проверка текущего кредитного плеча и его установка только при необходимости.
        Идемпотентная операция: предотвращает ошибку 110043 (leverage not modified).
        
        :param symbol: Торговая пара (например, 'BTCUSDT').
        :param target_leverage: Требуемое значение плеча.
        :return: True, если плечо успешно установлено или уже соответствует требуемому.
        """
        # 1. Быстрая проверка по локальному кэшу
        if self.leverage_cache.get(symbol) == target_leverage:
            return True

        # 2. Проверка реального состояния на бирже
        res = await self._api_call(self.session.get_positions, category="linear", symbol=symbol)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            current_lev = int(res['result']['list'][0]['leverage'])
            if current_lev == target_leverage:
                self.leverage_cache[symbol] = target_leverage
                return True

        # 3. Отправка запроса на изменение плеча
        res = await self._api_call(
            self.session.set_leverage,
            category="linear", symbol=symbol,
            buyLeverage=str(target_leverage),
            sellLeverage=str(target_leverage)
        )
        
        # Если успешно или биржа ответила, что плечо уже такое (защита от race condition)
        if res.get('retCode') == 0 or "110043" in str(res.get('retMsg', '')):
            self.leverage_cache[symbol] = target_leverage
            bot_logger.info(f"Плечо {target_leverage}x подтверждено для {symbol}")
            return True
            
        bot_logger.warning(f"Не удалось проверить/установить плечо для {symbol}")
        return True # Возвращаем True, чтобы не блокировать торговую логику

    def check_connection(self) -> bool:
        """
        Проверка связи с API Bybit путем запроса баланса USDT.
        
        :return: True при успешном подключении, иначе False.
        """
        try: 
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            return res.get('retCode') == 0
        except Exception: 
            return False

    def update_limit(self, new_limit: float):
        """Обновление лимита депозита в оперативной памяти и БД."""
        self.trade_limit = new_limit
        self.settings.set("trade_limit", new_limit)
        bot_logger.info(f"Лимит на сделку обновлен: ${new_limit}")

    def get_real_equity(self) -> float:
        """Получение реального баланса аккаунта (Equity) с учетом плавающего PNL."""
        try:
            res = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if res.get('retCode') == 0: 
                return float(res['result']['list'][0]['coin'][0]['equity'])
        except Exception: 
            pass
        return 0.0

    def load_active_positions(self):
        """Синхронизация локального кэша активных сделок с базой данных TradesDatabase."""
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
        """Асинхронный опрос биржи для получения нереализованного (плавающего) PNL по открытым позициям."""
        res = await self._api_call(self.session.get_positions, category="linear", settleCoin="USDT")
        if res.get('retCode') == 0:
            self.live_stats = {
                p['symbol']: {"unrealisedPnl": float(p['unrealisedPnl'])} 
                for p in res['result']['list'] if float(p['size']) > 0
            }

    async def _get_instrument_info(self, symbol: str) -> tuple[float, float]:
        """
        Получение параметров инструмента (минимальный шаг объема и цены).
        Использует кэширование для минимизации запросов к бирже.
        
        :return: Кортеж (qty_step, price_step). По умолчанию (0.001, 0.01) при ошибке.
        """
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
        """
        Универсальная математическая функция округления цены или объема 
        до требуемой точности биржи (в меньшую сторону, для безопасности).
        """
        precision = int(abs(math.log10(step))) if step < 1 else 0
        return round(math.floor(value / step) * step, precision)

    def _calc_qty(self, deposit: float, percent: float, leverage: int, price: float, qty_step: float) -> float:
        """
        Расчет объема ордера в монетах с учетом выделенного депозита, процента на шаг и плеча.
        
        :return: Округленный размер позиции (qty).
        """
        amount_usdt = deposit * (percent / 100.0)
        qty = (amount_usdt * leverage) / price
        return self._round_value(qty, qty_step)

    async def _place_limit(self, symbol: str, side: str, qty: float, price: float, reduce_only: bool = False) -> dict:
        """Отправка лимитного ордера (GTC). Используется для DCA и Take Profit."""
        return await self._api_call(
            self.session.place_order,
            category="linear", symbol=symbol, side=side, orderType="Limit",
            qty=str(qty), price=str(price), timeInForce="GTC", reduceOnly=reduce_only
        )

    async def _cancel_order_safe(self, symbol: str, order_id: str | None):
        """Безопасная отмена ордера по его ID. Игнорирует ошибки, если ордер уже закрыт."""
        if order_id:
            await self._api_call(self.session.cancel_order, category="linear", symbol=symbol, orderId=order_id)

    async def _get_open_order_ids(self, symbol: str) -> set:
        """Получение множества ID всех открытых (неисполненных) ордеров по монете."""
        res = await self._api_call(self.session.get_open_orders, category="linear", symbol=symbol)
        if res.get("retCode") == 0:
            return {o.get("orderId") for o in res["result"]["list"]}
        return set()

    async def _get_real_execution_data(self, order_id: str, symbol: str) -> tuple[float | None, float | None, float]:
        """
        Получение фактических данных об исполнении ордера (цена, объем, комиссия).
        Используется для точного расчета средней цены после проскальзываний.
        
        :return: Кортеж (Средняя цена исполнения, Стоимость в USDT, Комиссия).
        """
        await asyncio.sleep(1.2) # Небольшая задержка, чтобы биржа успела обновить данные
        res = await self._api_call(self.session.get_executions, category="linear", symbol=symbol, orderId=order_id)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            ex = res['result']['list']
            q = sum(float(e['execQty']) for e in ex)
            v = sum(float(e['execValue']) for e in ex)
            f = sum(float(e['execFee']) for e in ex)
            if q > 0: 
                return v / q, v, f
        return None, None, 0.0

    async def execute_signal(self, coin: str, signal_type: str, signal_price: float, target_price: float):
        """
        Внешний интерфейс для приема торговых сигналов от парсера.
        Проводит базовые проверки по WhiteList и передает управление в обработчик входа.
        """
        bot_logger.info(f"⚡ ТОРГОВЫЙ МОДУЛЬ: Получена команда {signal_type} для {coin}")
        
        coin_info = self.coins_db.get_coin(coin)
        if not coin_info or not coin_info["is_active"]: 
            return 
        
        # Подстановка алиаса биржи (например, 1000PEPEUSDT вместо PEPEUSDT)
        coin = coin_info.get("alias") or coin

        # Защита от дублирования сделок
        if self.db.get_trading_trade(coin):
            bot_logger.warning(f"Пропуск {signal_type} для {coin}: активная сделка уже существует.")
            return

        if signal_type == "OPEN":
            await self._handle_open_signal(coin)

    async def _handle_open_signal(self, coin: str):
        """
        Логика первичного входа в позицию по рыночной цене (Market Order).
        Инициализирует защитную сетку (выставляет первый TP и первый ордер DCA).
        """
        grid = self._get_dca_grid()
        leverage = int(self.settings.get("leverage", config.LEVERAGE))
        deposit = float(self.settings.get("trade_limit", self.trade_limit))
        
        # Установка плеча (идемпотентно)
        await self._ensure_leverage(coin, leverage)
        
        try:
            # Получение текущей цены актива
            res = await self._api_call(self.session.get_tickers, category="linear", symbol=coin)
            if res.get('retCode') != 0: 
                return
            
            price = float(res['result']['list'][0]['lastPrice'])
            qty_step, price_step = await self._get_instrument_info(coin)
            
            qty = self._calc_qty(deposit, grid["volumes"][0], leverage, price, qty_step)
            if qty <= 0: 
                return

            # Отправка рыночного ордера на вход (Step 0)
            order = await self._api_call(
                self.session.place_order, 
                category="linear", symbol=coin, side="Buy", orderType="Market", qty=str(qty)
            )
            
            if order.get('retCode') == 0:
                order_id = order['result']['orderId']
                
                # Получаем реальные данные исполнения, так как Market может скользить
                real_p, real_inv, exec_fee = await self._get_real_execution_data(order_id, coin)
                real_p = real_p or price
                real_inv = real_inv or (qty * price)
                
                # Запись новой позиции в локальную базу данных
                self.db.create_trade(coin, real_p, real_inv, 0.0, leverage, exec_fee)
                bot_logger.info(f"Исполнен вход {coin}. Цена: {real_p}.")

                # Выставление лимитного Take Profit
                tp_price = self._round_value(real_p * (1 + grid["tp_target"] / 100.0), price_step)
                tp_order = await self._place_limit(coin, "Sell", qty, tp_price, reduce_only=True)
                if tp_order.get("retCode") == 0:
                    self.db.set_tp_order_id(coin, tp_order["result"]["orderId"])

                # Выставление первого уровня усреднения (DCA_1)
                dca1_price = self._round_value(real_p * (1 - grid["levels"][0] / 100.0), price_step)
                dca1_qty = self._calc_qty(deposit, grid["volumes"][1], leverage, dca1_price, qty_step)
                dca_order = await self._place_limit(coin, "Buy", dca1_qty, dca1_price)
                if dca_order.get("retCode") == 0:
                    self.db.set_dca_order_id(coin, dca_order["result"]["orderId"])

                # Уведомление в Telegram
                await self.notifier.send(f"🟢 <b>ВХОД: {coin}</b>\nЦена: {real_p}\nTP: {tp_price}\nDCA_1: {dca1_price}")
                self.load_active_positions()
        except Exception as e:
            bot_logger.error(f"Ошибка OPEN {coin}: {e}")

    async def monitor_fills(self):
        """
        Бесконечный фоновый цикл (Daemon).
        Опрашивает статус лимитных ордеров (TP и DCA) и реагирует на их исполнение.
        """
        while True:
            await asyncio.sleep(2.0) # Задержка для предотвращения лимита API (Rate Limit)
            try:
                open_trades = self.db.get_open_trades()
                for trade in open_trades:
                    await self._process_trade_fills(trade)
            except Exception as e:
                bot_logger.error(f"Ошибка мониторинга: {e}")

    async def _process_trade_fills(self, trade: dict):
        """Определение, какой именно ордер исполнился по текущей сделке."""
        coin = trade["coin"]
        open_order_ids = await self._get_open_order_ids(coin)

        # Проверка: если ID ордера TP был в базе, но исчез с биржи -> он исполнился
        if trade["tp_order_id"] and trade["tp_order_id"] not in open_order_ids:
            await self._process_tp_execution(trade, coin)
            return

        # Проверка: если ID ордера DCA исчез с биржи -> произошло усреднение
        if trade["dca_order_id"] and trade["dca_order_id"] not in open_order_ids:
            await self._process_dca_execution(trade, coin)

    async def _process_tp_execution(self, trade: dict, coin: str):
        """Обработка успешного закрытия сделки по Take Profit."""
        res_p, _, fee = await self._get_real_execution_data(trade["tp_order_id"], coin)
        if res_p:
            # Расчет чистой прибыли и закрытие записи в БД
            _, _, net_pnl = self.db.close_trade(coin, res_p, fee)
            
            # Отменяем висящий ордер усреднения (чтобы не открыть случайно новую позицию)
            await self._cancel_order_safe(coin, trade["dca_order_id"])
            
            bot_logger.info(f"✅ TP Исполнен {coin}. PNL: ${net_pnl:.2f}")
            await self.notifier.send(f"✅ <b>TP: {coin}</b>\nЦена: {res_p}\nNet PNL: <b>{net_pnl:.2f}$</b>")
            self.load_active_positions()

    async def _process_dca_execution(self, trade: dict, coin: str):
        """
        Обработка усреднения (DCA).
        Отменяет старый TP, пересчитывает среднюю цену входа,
        и выставляет новые лимитные ордера на выход (TP) и следующее усреднение.
        """
        filled_p, filled_inv, fee = await self._get_real_execution_data(trade["dca_order_id"], coin)
        if not filled_p: 
            return
        
        next_step = int(trade["step"]) + 1
        
        # Обновление средней цены в БД на основе новых вливаний
        self.db.update_trade_dca(coin, next_step, filled_p, filled_inv, fee)
        updated = self.db.get_trading_trade(coin)
        
        # Отмена старого (неактуального) Take Profit
        await self._cancel_order_safe(coin, trade["tp_order_id"])
        
        qty_step, price_step = await self._get_instrument_info(coin)
        grid = self._get_dca_grid()
        leverage = int(trade["leverage"])
        deposit = float(self.settings.get("trade_limit", self.trade_limit))
        
        # Пересчет общего объема позиции для выставления нового TP
        total_qty = self._round_value(updated["total_inv"] / updated["avg_p"], qty_step)
        
        # Расчет и выставление нового TP от новой средней цены
        new_tp = self._round_value(updated["avg_p"] * (1 + grid["tp_target"] / 100.0), price_step)
        tp_res = await self._place_limit(coin, "Sell", total_qty, new_tp, reduce_only=True)
        if tp_res.get("retCode") == 0:
            self.db.set_tp_order_id(coin, tp_res["result"]["orderId"])
        
        # Расчет и выставление следующего DCA (Рекурсивно от цены текущего исполнения)
        if next_step < 3: # Ограничение сетки 3 шагами
            next_dev = grid["levels"][next_step]
            next_vol = grid["volumes"][next_step + 1]
            
            # Точка следующего закупа считается от цены только что сработавшего ордера (filled_p)
            next_p = self._round_value(filled_p * (1 - next_dev / 100.0), price_step)
            next_q = self._calc_qty(deposit, next_vol, leverage, next_p, qty_step)
            
            dca_res = await self._place_limit(coin, "Buy", next_q, next_p)
            if dca_res.get("retCode") == 0:
                self.db.set_dca_order_id(coin, dca_res["result"]["orderId"])
        
        await self.notifier.send(f"⚖️ <b>DCA #{next_step}: {coin}</b>\nСр.цена: {updated['avg_p']:.4f}\nНовый TP: {new_tp}")
        self.load_active_positions()