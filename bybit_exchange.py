"""
Модуль для взаимодействия с API биржи Bybit (Unified Trading Account).
Обеспечивает выставление ордеров, управление позициями, расчет сетки DCA 
и обработку торговых сигналов.
"""

import math
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Any, Tuple, Optional, Set, List

# Время последнего успешного ответа биржи — читается из GUI-потока (GIL-safe)
last_api_ok: Optional[datetime] = None

from pybit.unified_trading import HTTP
import aiosqlite

import config
from database import TradesDatabase, SettingsDatabase, CoinsDatabase
from database.trades import (
    ROLE_OPEN, ROLE_DCA, ROLE_TP,
    ORDER_ACTIVE, ORDER_FILLED, ORDER_CANCELLED, ORDER_REPLACED,
)
from logger import bot_logger

if TYPE_CHECKING:
    from notifier import Notifier


@dataclass
class InstrumentInfo:
    """Параметры торгового инструмента Bybit (lotSizeFilter + priceFilter)."""
    qty_step: float
    tick_size: float
    max_mkt_qty: float       # лимит market-ордера (lotSizeFilter.maxMktOrderQty)
    max_order_qty: float     # лимит limit-ордера (lotSizeFilter.maxOrderQty)
    min_order_qty: float


class BybitExchange:
    """
    Основной класс для работы с биржей Bybit.
    Управляет торговым циклом, кэшированием данных и мониторингом ордеров.
    """
    
    def __init__(self, initial_limit: float, trades_db: TradesDatabase, settings_db: SettingsDatabase, coins_db: CoinsDatabase, notifier: "Notifier") -> None:
        self.db = trades_db
        self.settings = settings_db
        self.coins_db = coins_db
        self.notifier = notifier
        
        self.session = HTTP(
            testnet=config.BYBIT_TESTNET, 
            api_key=config.BYBIT_API_KEY, 
            api_secret=config.BYBIT_API_SECRET
        )
        
        self.trade_limit = initial_limit
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.instruments: Dict[str, InstrumentInfo] = {}
        self.leverage_cache: Dict[str, int] = {}
        self.live_stats: Dict[str, Dict[str, float]] = {}

        self._api_error_streak: int = 0
        self._api_disconnected: bool = False

        # Счётчик циклов мониторинга для grace-period перед STUCK
        # ключ = (trade_id, role), значение = число циклов с mix FILLED+ACTIVE
        self._stuck_counters: Dict[Tuple[int, str], int] = {}

        bot_logger.info("Биржевой модуль BybitExchange инициализирован.")

    async def _init_settings(self) -> None:
        """Асинхронная загрузка стартовых настроек из БД (вызывается извне)."""
        saved_limit = await self.settings.get("trade_limit")
        if saved_limit:
            self.trade_limit = float(saved_limit)

        # Прелоад кэша инструментов: при первом сигнале на лимиты не идём в API
        try:
            coins_rows = await self.coins_db.get_all()
            active = [c['alias'] or c['coin'] for c in coins_rows if c.get('is_active')]
            if active:
                await self.refresh_instruments_cache(active)
        except Exception as e:
            bot_logger.error(f"EXCHANGE: прелоад инструментов не удался: {e}")

    async def refresh_instruments_cache(self, coins: List[str]) -> None:
        """Параллельно перечитать параметры инструментов с биржи."""
        if not coins:
            return
        results = await asyncio.gather(
            *(self._fetch_instrument(c) for c in coins),
            return_exceptions=True
        )
        ok = sum(1 for r in results if isinstance(r, InstrumentInfo))
        fail = len(coins) - ok
        if fail:
            bot_logger.warning(
                f"EXCHANGE: refresh инструментов — {ok}/{len(coins)} успешно, {fail} ошибок"
            )
        else:
            bot_logger.info(f"Кэш инструментов прогрет: {ok} монет")

    async def _instruments_refresh_loop(self) -> None:
        """Фоновое обновление кэша лимитов раз в config.INSTRUMENTS_REFRESH_INTERVAL_SEC."""
        while True:
            await asyncio.sleep(config.INSTRUMENTS_REFRESH_INTERVAL_SEC)
            try:
                coins_rows = await self.coins_db.get_all()
                active = [c['alias'] or c['coin'] for c in coins_rows if c.get('is_active')]
                if active:
                    await self.refresh_instruments_cache(active)
            except Exception as e:
                bot_logger.error(f"EXCHANGE: периодический refresh не удался: {e}")

    async def _get_dca_grid(self) -> Dict[str, Any]:
        """Загрузка динамических настроек сетки DCA и Take Profit из базы данных."""
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
        """Централизованная обертка для асинхронного вызова синхронных методов pybit."""
        try:
            res = await asyncio.to_thread(func, *args, **kwargs)
            if isinstance(res, dict) and res.get('retCode') != 0:
                bot_logger.error(f"API Ошибка: {res.get('retMsg')} (Код: {res.get('retCode')})")
                return res  # type: ignore
            # Успешный вызов — фиксируем время и сбрасываем счётчик
            import bybit_exchange as _self_mod
            _self_mod.last_api_ok = datetime.now()
            if self._api_disconnected:
                self._api_disconnected = False
                bot_logger.info("✅ Связь с биржей восстановлена.")
                await self.notifier.send("✅ <b>Связь с биржей восстановлена.</b>")
            self._api_error_streak = 0
            return res  # type: ignore
        except Exception as e:
            bot_logger.error(f"Сетевая/Внутренняя ошибка API: {e}")
            self._api_error_streak += 1
            if self._api_error_streak >= 3 and not self._api_disconnected:
                self._api_disconnected = True
                bot_logger.warning("🔴 Биржа недоступна — несколько запросов подряд завершились ошибкой.")
                await self.notifier.send("🔴 <b>Биржа недоступна.</b> Проверяю соединение...")
            return {'retCode': -1, 'retMsg': str(e)}

    async def _ensure_leverage(self, symbol: str, target_leverage: int) -> bool:
        """Проверка текущего кредитного плеча и его установка только при необходимости."""
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
        """Проверка связи с API Bybit."""
        res = await self._api_call(self.session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        return res.get('retCode') == 0

    async def update_limit(self, new_limit: float) -> None:
        self.trade_limit = new_limit
        await self.settings.set("trade_limit", new_limit)
        bot_logger.info(f"Лимит на сделку обновлен: ${new_limit}")

    async def get_real_equity(self) -> float:
        equity, _ = await self.get_balance_info()
        return equity

    async def get_balance_info(self) -> Tuple[float, float]:
        res = await self._api_call(self.session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            coin_data = res['result']['list'][0]['coin'][0]
            equity = float(coin_data.get('equity', 0.0))
            pos_margin = float(coin_data.get('totalPositionIM', 0.0))
            order_margin = float(coin_data.get('totalOrderIM', 0.0))
            available = max(0.0, equity - pos_margin - order_margin)
            return equity, available
        return 0.0, 0.0

    async def load_active_positions(self) -> None:
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

    async def fetch_live_stats(self) -> None:
        res = await self._api_call(self.session.get_positions, category="linear", settleCoin="USDT")
        if res.get('retCode') == 0:
            self.live_stats = {
                p['symbol']: {
                    "unrealisedPnl": float(p['unrealisedPnl']),
                    "markPrice": float(p['markPrice'])
                } 
                for p in res['result']['list'] if float(p['size']) > 0
            }

    async def _get_instrument_info(self, symbol: str) -> InstrumentInfo:
        """Параметры инструмента: при отсутствии в кэше — однократный запрос к бирже."""
        if symbol not in self.instruments:
            await self._fetch_instrument(symbol)
        return self.instruments.get(symbol) or InstrumentInfo(
            qty_step=0.001, tick_size=0.01,
            max_mkt_qty=0.0, max_order_qty=0.0, min_order_qty=0.0,
        )

    async def _fetch_instrument(self, symbol: str) -> Optional[InstrumentInfo]:
        """Однократный запрос параметров инструмента, заполняет кэш."""
        res = await self._api_call(
            self.session.get_instruments_info, category="linear", symbol=symbol
        )
        if res.get('retCode') != 0:
            return None
        lst = res.get('result', {}).get('list', [])
        if not lst:
            return None
        info = lst[0]
        lot = info.get('lotSizeFilter', {}) or {}
        prc = info.get('priceFilter', {}) or {}
        try:
            max_order_qty = float(lot.get('maxOrderQty', 0) or 0)
            # У некоторых линейных перпов maxMktOrderQty отсутствует — используем maxOrderQty
            max_mkt_qty = float(lot.get('maxMktOrderQty', max_order_qty) or max_order_qty)
            instrument = InstrumentInfo(
                qty_step=float(lot.get('qtyStep', 0.001) or 0.001),
                tick_size=float(prc.get('tickSize', 0.01) or 0.01),
                max_mkt_qty=max_mkt_qty,
                max_order_qty=max_order_qty,
                min_order_qty=float(lot.get('minOrderQty', 0) or 0),
            )
        except (TypeError, ValueError) as e:
            bot_logger.warning(f"EXCHANGE: не удалось распарсить параметры {symbol}: {e}")
            return None
        self.instruments[symbol] = instrument
        return instrument

    def _round_value(self, value: float, step: float) -> float:
        precision = int(abs(math.log10(step))) if step < 1 else 0
        return round(math.floor(value / step) * step, precision)

    def _calc_qty(self, deposit: float, percent: float, leverage: int, price: float, qty_step: float) -> float:
        amount_usdt = deposit * (percent / 100.0)
        qty = (amount_usdt * leverage) / price
        return self._round_value(qty, qty_step)

    def _split_qty(
        self,
        total: float,
        max_per_order: float,
        step: float,
        min_qty: float = 0.0,
    ) -> List[float]:
        """
        Разбивает qty на чанки ≤ max_per_order, каждый кратный step.
        Сумма чанков = total (с точностью до step).
        Если последний чанк < min_qty — сливается с предыдущим (если влезает в max).
        Если total ≤ max_per_order — возвращает [total].
        """
        total = self._round_value(total, step)
        if total <= 0:
            return []
        if max_per_order <= 0 or total <= max_per_order:
            return [total]

        n = math.ceil(total / max_per_order)
        base = self._round_value(total / n, step)
        if base <= 0:
            return [total]
        # На случай если округление вверх вытолкнуло base за max — снижаем до max и пересчитываем
        if base > max_per_order:
            base = self._round_value(max_per_order, step)
            n = math.ceil(total / base) if base > 0 else 1

        chunks: List[float] = [base] * (n - 1)
        last = self._round_value(total - base * (n - 1), step)
        if last <= 0:
            return chunks
        # Если хвост < min_qty — слить с предыдущим, если это не выходит за max
        if min_qty and chunks and last < min_qty:
            merged = self._round_value(chunks[-1] + last, step)
            if merged <= max_per_order:
                chunks[-1] = merged
                return chunks
        chunks.append(last)
        return chunks

    async def _place_limit(self, symbol: str, side: str, qty: float, price: float, reduce_only: bool = False) -> Dict[str, Any]:
        return await self._api_call(
            self.session.place_order,
            category="linear", symbol=symbol, side=side, orderType="Limit",
            qty=str(qty), price=str(price), timeInForce="GTC", reduceOnly=reduce_only
        )

    async def _cancel_order_safe(self, symbol: str, order_id: Optional[str]) -> None:
        if order_id:
            await self._api_call(self.session.cancel_order, category="linear", symbol=symbol, orderId=order_id)

    async def _get_open_order_ids(self, symbol: str) -> Set[str]:
        res = await self._api_call(self.session.get_open_orders, category="linear", symbol=symbol)
        if res.get("retCode") == 0:
            return {o.get("orderId") for o in res["result"]["list"]}
        return set()

    async def _get_real_execution_data(self, order_id: str, symbol: str) -> Tuple[Optional[float], Optional[float], float]:
        await asyncio.sleep(config.EXEC_DATA_DELAY)
        res = await self._api_call(self.session.get_executions, category="linear", symbol=symbol, orderId=order_id)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            ex = res['result']['list']
            q = sum(float(e['execQty']) for e in ex)
            v = sum(float(e['execValue']) for e in ex)
            f = sum(float(e['execFee']) for e in ex)
            if q > 0:
                return v / q, v, f
        return None, None, 0.0

    async def _fetch_funding_fee(self, symbol: str, since_iso: str) -> float:
        """
        Сумма фандинга по символу за период от since_iso до now.
        Возвращает положительное число = расход (списано со счёта).
        Запрашивает get_transaction_log type=SETTLEMENT с пагинацией.
        """
        try:
            dt = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            start_ms = int(dt.timestamp() * 1000)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        except Exception as e:
            bot_logger.warning(f"EXCHANGE: Не удалось распарсить created_at='{since_iso}' для {symbol}: {e}")
            return 0.0

        total = 0.0
        cursor: Optional[str] = None
        for _ in range(10):  # safety: max 500 записей
            params: Dict[str, Any] = {
                "accountType": "UNIFIED",
                "category": "linear",
                "currency": "USDT",
                "type": "SETTLEMENT",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 50,
            }
            if cursor:
                params["cursor"] = cursor
            res = await self._api_call(self.session.get_transaction_log, **params)
            if res.get('retCode') != 0:
                break
            result = res.get('result', {}) or {}
            items = result.get('list', []) or []
            for it in items:
                if it.get('symbol') == symbol:
                    try:
                        total += float(it.get('funding', 0) or 0)
                    except (TypeError, ValueError):
                        continue
            cursor = result.get('nextPageCursor') or None
            if not cursor or len(items) < 50:
                break

        # API возвращает отрицательное значение при списании — переводим в положительный расход
        return -total

    async def _sync_position_from_exchange(
        self, symbol: str
    ) -> Tuple[float, float, float]:
        """
        Реальное состояние позиции с биржи: (size, avg_price, position_value).
        Источник истины вместо самостоятельного расчёта средней цены.
        """
        res = await self._api_call(self.session.get_positions, category="linear", symbol=symbol)
        if res.get('retCode') == 0 and res.get('result', {}).get('list'):
            try:
                p = res['result']['list'][0]
                size = float(p.get('size', 0) or 0)
                avg = float(p.get('avgPrice', 0) or 0)
                val = float(p.get('positionValue', 0) or 0)
                if val <= 0 and size > 0 and avg > 0:
                    val = size * avg
                return size, avg, val
            except (TypeError, ValueError, IndexError):
                pass
        return 0.0, 0.0, 0.0

    async def _fetch_realized_pnl(
        self, symbol: str, since_iso: str
    ) -> Tuple[float, float, float, float]:
        """
        Суммирует РЕАЛЬНЫЕ закрытия позиции с биржи за период [since_iso..now]
        через get_closed_pnl. Устойчиво к частичным TP (несколько закрытий одной сделки).

        Возвращает (gross_pnl, total_fee, net_closed, last_exit_price):
          net_closed = Σ closedPnl            (нетто после комиссий, как считает биржа)
          gross_pnl  = Σ(cumExitValue − cumEntryValue)  (ценовая разница до комиссий)
          total_fee  = gross_pnl − net_closed (суммарная комиссия open+close)
        """
        try:
            dt = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            start_ms = int(dt.timestamp() * 1000)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        except Exception as e:
            bot_logger.warning(f"EXCHANGE: _fetch_realized_pnl парсинг даты '{since_iso}': {e}")
            return 0.0, 0.0, 0.0, 0.0

        gross = 0.0
        net_closed = 0.0
        last_exit = 0.0
        last_ts = 0
        cursor: Optional[str] = None
        for _ in range(10):
            params: Dict[str, Any] = {
                "category": "linear", "symbol": symbol,
                "startTime": start_ms, "endTime": end_ms, "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            res = await self._api_call(self.session.get_closed_pnl, **params)
            if res.get('retCode') != 0:
                break
            result = res.get('result', {}) or {}
            items = result.get('list', []) or []
            for it in items:
                try:
                    net_closed += float(it.get('closedPnl', 0) or 0)
                    gross += float(it.get('cumExitValue', 0) or 0) - float(it.get('cumEntryValue', 0) or 0)
                    ts = int(it.get('updatedTime', 0) or 0)
                    if ts >= last_ts:
                        last_ts = ts
                        last_exit = float(it.get('avgExitPrice', 0) or 0)
                except (TypeError, ValueError):
                    continue
            cursor = result.get('nextPageCursor') or None
            if not cursor or len(items) < 100:
                break

        total_fee = gross - net_closed
        return gross, total_fee, net_closed, last_exit

    # ------------------------------------------------------------------
    # Chunked helpers (разбивка ордеров на куски по лимитам инструмента)
    # ------------------------------------------------------------------

    async def _place_market_chunked(
        self,
        symbol: str,
        side: str,
        total_qty: float,
        reduce_only: bool = False,
    ) -> List[str]:
        """
        Один или несколько market-ордеров. Возвращает список order_id успешно
        выставленных ордеров. При retCode != 0 — алёрт в TG и обрыв серии.
        """
        instrument = await self._get_instrument_info(symbol)
        chunks = self._split_qty(
            total_qty,
            instrument.max_mkt_qty if instrument.max_mkt_qty > 0 else total_qty,
            instrument.qty_step,
            min_qty=instrument.min_order_qty,
        )
        if not chunks:
            return []
        if len(chunks) > 1:
            bot_logger.info(
                f"Чанкинг {symbol} {side} MARKET: qty={total_qty} разбит на {len(chunks)}: {chunks}"
            )
        order_ids: List[str] = []
        for i, chunk in enumerate(chunks):
            res = await self._api_call(
                self.session.place_order,
                category="linear", symbol=symbol, side=side,
                orderType="Market", qty=str(chunk),
                reduceOnly=reduce_only,
            )
            if res.get("retCode") == 0 and res.get("result", {}).get("orderId"):
                order_ids.append(res["result"]["orderId"])
            else:
                msg = f"⚠️ Ордер не выставлен: {symbol} {side} MARKET {chunk} — {res.get('retMsg', '?')} (код {res.get('retCode')})"
                bot_logger.error(msg)
                try:
                    await self.notifier.send(msg)
                except Exception:
                    pass
                break  # серия прервана: остаток не отправляем
            if i < len(chunks) - 1:
                await asyncio.sleep(0.1)  # rate-limit запас
        return order_ids

    async def _place_limit_chunked(
        self,
        symbol: str,
        side: str,
        total_qty: float,
        price: float,
        reduce_only: bool = False,
    ) -> List[str]:
        """Один или несколько limit-ордеров на одну цену. List[order_id]."""
        instrument = await self._get_instrument_info(symbol)
        chunks = self._split_qty(
            total_qty,
            instrument.max_order_qty if instrument.max_order_qty > 0 else total_qty,
            instrument.qty_step,
            min_qty=instrument.min_order_qty,
        )
        if not chunks:
            return []
        if len(chunks) > 1:
            bot_logger.info(
                f"Чанкинг {symbol} {side} LIMIT @ {price}: qty={total_qty} разбит на {len(chunks)}: {chunks}"
            )
        order_ids: List[str] = []
        for i, chunk in enumerate(chunks):
            res = await self._place_limit(symbol, side, chunk, price, reduce_only=reduce_only)
            if res.get("retCode") == 0 and res.get("result", {}).get("orderId"):
                order_ids.append(res["result"]["orderId"])
            else:
                msg = f"⚠️ Ордер не выставлен: {symbol} {side} LIMIT {chunk}@{price} — {res.get('retMsg', '?')} (код {res.get('retCode')})"
                bot_logger.error(msg)
                try:
                    await self.notifier.send(msg)
                except Exception:
                    pass
                break
            if i < len(chunks) - 1:
                await asyncio.sleep(0.1)
        return order_ids

    async def _cancel_orders_safe(self, symbol: str, order_ids: List[str]) -> None:
        """Безопасная отмена набора ордеров — последовательная, с игнором ошибок (уже отменён и т.п.)."""
        for oid in order_ids:
            if not oid:
                continue
            try:
                await self._api_call(
                    self.session.cancel_order, category="linear", symbol=symbol, orderId=oid
                )
            except Exception as e:
                bot_logger.warning(f"EXCHANGE: не удалось отменить {symbol} {oid}: {e}")

    async def _get_aggregated_execution_data(
        self,
        symbol: str,
        order_ids: List[str],
        since_iso: Optional[str] = None,
    ) -> Tuple[Optional[float], Optional[float], float]:
        """
        Агрегирует execution data по списку order_ids: avg_p (взвешенная), total_value, total_fee.
        Один запрос get_executions по символу за период, фильтрация по orderId в Python.
        """
        if not order_ids:
            return None, None, 0.0
        await asyncio.sleep(config.EXEC_DATA_DELAY)

        target = set(order_ids)
        params: Dict[str, Any] = {"category": "linear", "symbol": symbol, "limit": 100}
        if since_iso:
            try:
                dt = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                params["startTime"] = int(dt.timestamp() * 1000)
            except Exception:
                pass

        total_q = 0.0
        total_v = 0.0
        total_f = 0.0
        cursor: Optional[str] = None
        for _ in range(10):
            if cursor:
                params["cursor"] = cursor
            res = await self._api_call(self.session.get_executions, **params)
            if res.get('retCode') != 0:
                break
            result = res.get('result', {}) or {}
            items = result.get('list', []) or []
            for ex in items:
                if ex.get('orderId') not in target:
                    continue
                try:
                    total_q += float(ex.get('execQty', 0) or 0)
                    total_v += float(ex.get('execValue', 0) or 0)
                    total_f += float(ex.get('execFee', 0) or 0)
                except (TypeError, ValueError):
                    continue
            cursor = result.get('nextPageCursor') or None
            if not cursor or len(items) < 100:
                break

        if total_q > 0:
            return total_v / total_q, total_v, total_f
        return None, None, total_f

    async def _classify_orders(
        self, symbol: str, order_ids: List[str]
    ) -> Dict[str, str]:
        """
        Определить финальный статус ордеров через get_order_history.
        Возвращает {order_id: статус Bybit}, например 'Filled' / 'Cancelled' /
        'PartiallyFilled' / 'New'. Если не нашли — 'Unknown'.
        """
        if not order_ids:
            return {}
        result_map: Dict[str, str] = {oid: 'Unknown' for oid in order_ids}
        target = set(order_ids)
        params: Dict[str, Any] = {"category": "linear", "symbol": symbol, "limit": 50}
        cursor: Optional[str] = None
        for _ in range(5):
            if cursor:
                params["cursor"] = cursor
            res = await self._api_call(self.session.get_order_history, **params)
            if res.get('retCode') != 0:
                break
            result = res.get('result', {}) or {}
            items = result.get('list', []) or []
            for o in items:
                oid = o.get('orderId')
                if oid in target:
                    result_map[oid] = o.get('orderStatus') or 'Unknown'
            if all(v != 'Unknown' for v in result_map.values()):
                return result_map
            cursor = result.get('nextPageCursor') or None
            if not cursor or len(items) < 50:
                break
        return result_map

    async def execute_signal(self, coin: str, signal_type: str, signal_price: float, target_price: float) -> None:
        bot_logger.info(f"⚡ ТОРГОВЫЙ МОДУЛЬ: Получена команда {signal_type} для {coin}")
        
        coin_info = await self.coins_db.get_coin(coin)
        if not coin_info or not coin_info["is_active"]: 
            return 
        
        coin_alias = coin_info.get("alias") or coin

        if await self.db.get_trading_trade(coin_alias):
            bot_logger.warning(f"Пропуск {signal_type} для {coin_alias}: активная сделка уже существует.")
            return

        if signal_type == "OPEN":
            # Проверка главного тумблера "Разрешить новые входы"
            allow_open = await self.settings.get("allow_open", "False") == "True"
            if not allow_open:
                bot_logger.info(f"Пропуск OPEN для {coin_alias}: торговля отключена в настройках.")
                return
                
            # Проверка лимита активных сделок
            max_trades = int(await self.settings.get("max_active_trades", "3"))
            await self.load_active_positions() # Синхронизируем количество перед проверкой
            active_count = len(self.active_positions)
            
            if active_count >= max_trades:
                bot_logger.warning(f"⛔ Пропуск OPEN для {coin_alias}: достигнут лимит активных сделок ({active_count}/{max_trades}).")
                return

            await self._handle_open_signal(coin_alias)

    async def _handle_open_signal(self, coin: str) -> None:
        grid = await self._get_dca_grid()
        leverage = int(await self.settings.get("leverage", str(config.LEVERAGE)))
        deposit = float(await self.settings.get("trade_limit", str(self.trade_limit)))

        await self._ensure_leverage(coin, leverage)

        try:
            res = await self._api_call(self.session.get_tickers, category="linear", symbol=coin)
            if res.get('retCode') != 0:
                return

            price = float(res['result']['list'][0]['lastPrice'])
            instrument = await self._get_instrument_info(coin)
            qty_step = instrument.qty_step
            price_step = instrument.tick_size

            qty = self._calc_qty(deposit, grid["volumes"][0], leverage, price, qty_step)
            if qty <= 0:
                return

            # === MARKET-вход (с разбивкой на чанки если qty > maxMktOrderQty) ===
            open_ids = await self._place_market_chunked(coin, "Buy", qty)
            if not open_ids:
                bot_logger.error(f"Ошибка OPEN {coin}: ни один чанк market-входа не выставлен")
                return

            real_p, real_inv, exec_fee = await self._get_aggregated_execution_data(coin, open_ids)
            real_p = real_p or price
            real_inv = real_inv or (qty * price)

            trade_id = await self.db.create_trade(coin, real_p, real_inv, 0.0, leverage, exec_fee)
            # OPEN-ордера market исполнились сразу — фиксируем как FILLED
            await self.db.add_orders(trade_id, ROLE_OPEN, open_ids, qty=qty)
            await self.db.mark_orders_status(open_ids, ORDER_FILLED)

            bot_logger.info(f"Исполнен вход {coin}. Цена: {real_p}. trade_id={trade_id}")

            # === LIMIT TP (с разбивкой если qty > maxOrderQty) ===
            tp_price = self._round_value(real_p * (1 + grid["tp_target"] / 100.0), price_step)
            tp_ids = await self._place_limit_chunked(coin, "Sell", qty, tp_price, reduce_only=True)
            if tp_ids:
                await self.db.add_orders(trade_id, ROLE_TP, tp_ids, qty=qty)

            # === LIMIT DCA-1 ===
            allow_dca = await self.settings.get("allow_dca", "False") == "True"
            dca1_price = self._round_value(real_p * (1 - grid["levels"][0] / 100.0), price_step)

            if allow_dca:
                dca1_qty = self._calc_qty(deposit, grid["volumes"][1], leverage, dca1_price, qty_step)
                dca_ids = await self._place_limit_chunked(coin, "Buy", dca1_qty, dca1_price)
                if dca_ids:
                    await self.db.add_orders(trade_id, ROLE_DCA, dca_ids, step=1, qty=dca1_qty)

            await self.notifier.send(
                f"🟢 <b>ВХОД: {coin}</b>\nЦена: {real_p}\nОбъем: {real_inv:.2f} USDT\n"
                f"TP: {tp_price}\nПлановый DCA_1: {dca1_price}"
            )
            await self.load_active_positions()
        except Exception as e:
            bot_logger.error(f"Ошибка OPEN {coin}: {e}")

    async def monitor_fills(self) -> None:
        while True:
            await asyncio.sleep(config.MONITOR_INTERVAL)
            try:
                open_trades = await self.db.get_open_trades()
                for trade in open_trades:
                    await asyncio.sleep(0.1)
                    await self._process_trade_fills(trade)
            except Exception as e:
                bot_logger.error(f"Ошибка мониторинга: {e}")

    async def _process_trade_fills(self, trade: aiosqlite.Row) -> None:
        """
        Детектирует исполнение TP / DCA с учётом множественных chunked-ордеров.
        Различает FILLED и CANCELLED через get_order_history. Закрывает сделку только
        когда ВСЕ TP-чанки перешли в FILLED/CANCELLED. При зависании > STUCK_GRACE_CYCLES
        — сделка переходит в STUCK + алёрт.
        """
        coin = trade["coin"]
        trade_id = trade["id"]
        open_ids = await self._get_open_order_ids(coin)

        # Проверяем TP, затем DCA. Если TP исполнен — DCA уже не важен (сделка закрывается).
        for role in (ROLE_TP, ROLE_DCA):
            active_rows = await self.db.get_active_orders(trade_id, role)
            if not active_rows:
                continue

            active_ids = [r['order_id'] for r in active_rows]
            gone_ids = [oid for oid in active_ids if oid not in open_ids]
            if not gone_ids:
                # Все ACTIVE по этой роли всё ещё в open — ничего не делаем
                self._stuck_counters.pop((trade_id, role), None)
                continue

            # Классифицируем "ушедшие" из open_orders
            statuses = await self._classify_orders(coin, gone_ids)
            filled = [oid for oid, s in statuses.items() if s == 'Filled']
            cancelled = [
                oid for oid, s in statuses.items()
                if s in ('Cancelled', 'Rejected', 'Deactivated')
            ]
            unknown = [
                oid for oid, s in statuses.items()
                if oid not in filled and oid not in cancelled
            ]

            if cancelled:
                await self.db.mark_orders_status(cancelled, ORDER_CANCELLED)
                msg = (
                    f"⚠️ {coin}: {role}-ордера отменены вне бота ({len(cancelled)} шт.) — "
                    f"возможно ручное вмешательство"
                )
                bot_logger.warning(msg)
                try:
                    await self.notifier.send(msg)
                except Exception:
                    pass
            if filled:
                await self.db.mark_orders_status(filled, ORDER_FILLED)

            # Состояние после обновления: сколько ACTIVE осталось по роли
            remaining = await self.db.get_active_orders(trade_id, role)

            if not remaining and filled:
                # Все ордера роли в финальном статусе и хотя бы один исполнен → процессим
                self._stuck_counters.pop((trade_id, role), None)
                if role == ROLE_TP:
                    await self._process_tp_execution(trade, coin, filled)
                    return  # сделка закрыта
                elif role == ROLE_DCA:
                    await self._process_dca_execution(trade, coin, filled)
                    return  # после DCA-roll выходим — следующая итерация monitor проверит свежее состояние
            elif remaining and filled:
                # Частичное исполнение: есть и FILLED, и оставшиеся ACTIVE — это аномалия
                key = (trade_id, role)
                self._stuck_counters[key] = self._stuck_counters.get(key, 0) + 1
                if self._stuck_counters[key] >= config.STUCK_GRACE_CYCLES:
                    bot_logger.error(
                        f"🔴 {coin}: {role} частично исполнен ({len(filled)} FILLED, "
                        f"{len(remaining)} ACTIVE) > {config.STUCK_GRACE_CYCLES} циклов → STUCK"
                    )
                    await self.db.set_trade_status(coin, 'STUCK')
                    try:
                        await self.notifier.send(
                            f"🔴 <b>STUCK: {coin}</b>\nЧастичное исполнение {role}: "
                            f"{len(filled)} FILLED + {len(remaining)} ACTIVE.\nТребуется ручное вмешательство."
                        )
                    except Exception:
                        pass
                    self._stuck_counters.pop(key, None)
            elif unknown and not filled:
                # Все ушли, но статусы непонятны — подождём следующий цикл (BybitAPI лагает)
                bot_logger.debug(
                    f"{coin} {role}: {len(unknown)} ордеров со статусом Unknown — повтор на след. цикле"
                )

    async def _process_tp_execution(
        self, trade: aiosqlite.Row, coin: str, tp_filled_ids: List[str]
    ) -> None:
        """Закрытие сделки по TP. PnL берётся реально с биржи (Σ closedPnl)."""
        if not tp_filled_ids:
            return
        await self._finalize_closed_trade(trade, coin, reason="TP")

    async def _finalize_closed_trade(
        self, trade: aiosqlite.Row, coin: str, reason: str = "TP"
    ) -> None:
        """
        Финализирует закрытую сделку. БИРЖА — ИСТОЧНИК ИСТИНЫ:
        net_pnl = Σ closedPnl за период сделки − фандинг. Корректно учитывает
        частичные TP (несколько закрытий одной сделки) и любые расхождения учёта.

        ВАЖНО: перед закрытием проверяем, что позиция на бирже реально опустела.
        Если TP закрыл не весь объём (округление qty / частичное исполнение) —
        НЕ списываем сделку, а перевыставляем TP на оставшийся объём.
        """
        trade_id = trade['id']

        # Даём бирже зафиксировать исполнение и читаем реальный остаток позиции
        await asyncio.sleep(config.EXEC_DATA_DELAY)
        size, avg, value = await self._sync_position_from_exchange(coin)
        instrument = await self._get_instrument_info(coin)
        min_qty = instrument.min_order_qty

        # Остаток считается торгуемым, если size >= minOrderQty (или minOrderQty неизвестен).
        # Меньший остаток = «пыль», его нельзя выставить лимитным ордером — игнорируем.
        remainder_tradeable = size > 0 and (size >= min_qty if min_qty > 0 else True)

        # Позиция НЕ закрыта полностью — перевыставляем TP на остаток
        if remainder_tradeable and avg > 0:
            grid = await self._get_dca_grid()
            price_step = instrument.tick_size
            new_tp = self._round_value(avg * (1 + grid["tp_target"] / 100.0), price_step)

            new_tp_ids = await self._place_limit_chunked(
                coin, "Sell", size, new_tp, reduce_only=True
            )
            old_tp_ids = await self.db.replace_active_orders(
                trade_id, ROLE_TP, new_tp_ids, qty=size
            )
            if old_tp_ids:
                await self._cancel_orders_safe(coin, old_tp_ids)
            # Синхронизируем учёт с реальным остатком
            await self.db.sync_position(coin, int(trade["step"]), avg, value)

            msg = (
                f"⚠️ {coin}: TP закрыл не весь объём, остаток size={size} "
                f"перевыставлен на TP @ {new_tp}"
            )
            bot_logger.warning(msg)
            try:
                await self.notifier.send(
                    f"⚠️ <b>{coin}</b>: частичное закрытие.\n"
                    f"Остаток {size} перевыставлен на TP @ {new_tp}"
                )
            except Exception:
                pass
            await self.load_active_positions()
            return  # сделку НЕ закрываем — ждём добивания остатка

        # === Позиция реально пуста (или остаток ниже minOrderQty = пыль) → финализируем ===

        # Отменяем все оставшиеся ACTIVE-ордера сделки (DCA/TP-остатки)
        active = await self.db.get_active_orders(trade_id)
        if active:
            ids = [r['order_id'] for r in active]
            await self._cancel_orders_safe(coin, ids)
            await self.db.mark_orders_status(ids, ORDER_CANCELLED)

        gross, total_fee, net_closed, last_exit = await self._fetch_realized_pnl(
            coin, trade["created_at"]
        )
        funding_fee = await self._fetch_funding_fee(coin, trade["created_at"])
        # closedPnl Bybit УЖЕ включает все комиссии И фандинг — это финальный net.
        # Фандинг тянем отдельно ТОЛЬКО для разбивки O/F/C в дашборде, НЕ вычитаем повторно.
        net_pnl = net_closed

        exit_price = last_exit or trade["avg_p"] or 0.0
        await self.db.close_trade_realized(coin, exit_price, gross, net_pnl, funding_fee)

        dust_note = f", пыль size={size} проигнорирована" if size > 0 else ""
        bot_logger.info(
            f"✅ Закрыта {coin} ({reason}). Net PNL: ${net_pnl:.2f} "
            f"(gross ${gross:.2f}, fee ${total_fee:.2f} вкл. funding ${funding_fee:.4f}{dust_note})"
        )
        await self.notifier.send(
            f"✅ <b>Закрыта: {coin}</b>\nЦена выхода: {exit_price}\nNet PNL: <b>{net_pnl:.2f}$</b>"
        )
        await self.load_active_positions()

    async def _process_dca_execution(
        self, trade: aiosqlite.Row, coin: str, dca_filled_ids: List[str]
    ) -> None:
        """Обработка исполнения DCA-чанков на одном шаге."""
        if not dca_filled_ids:
            return
        filled_p, filled_inv, fee = await self._get_aggregated_execution_data(
            coin, dca_filled_ids, since_iso=trade["created_at"]
        )
        if not filled_p:
            return

        trade_id = trade['id']
        next_step = int(trade["step"]) + 1

        instrument = await self._get_instrument_info(coin)
        qty_step = instrument.qty_step
        price_step = instrument.tick_size
        grid = await self._get_dca_grid()
        leverage = int(trade["leverage"])
        deposit = float(await self.settings.get("trade_limit", str(self.trade_limit)))

        # === БИРЖА — ИСТОЧНИК ИСТИНЫ ===
        # Берём реальные avgPrice/positionValue/size с биржи вместо самостоятельного
        # пересчёта средней. Это устойчиво к частичному исполнению TP до DCA.
        real_qty, real_avg, real_value = await self._sync_position_from_exchange(coin)

        if real_qty <= 0:
            # Позиция на бирже пуста (например, TP полностью исполнился до DCA, а DCA-ордер
            # успел частично залиться и тут же закрылся). Закрываем сделку по факту.
            bot_logger.warning(
                f"⚠️ DCA {coin}: позиция на бирже пуста (size=0) — закрываю сделку по факту"
            )
            await self._finalize_closed_trade(trade, coin)
            return

        # Синхронизируем БД с реальным состоянием позиции; комиссию DCA-входа копим в open_fee
        await self.db.sync_position(coin, next_step, real_avg, real_value, add_open_fee=fee)
        updated = await self.db.get_trading_trade(coin)
        if not updated:
            return

        total_qty = real_qty

        # Новый TP по реальной средней цене с биржи
        new_tp = self._round_value(real_avg * (1 + grid["tp_target"] / 100.0), price_step)
        new_tp_ids = await self._place_limit_chunked(coin, "Sell", total_qty, new_tp, reduce_only=True)

        # Атомарная замена старых TP на новые; возвращает старые order_ids для отмены на бирже
        old_tp_ids = await self.db.replace_active_orders(trade_id, ROLE_TP, new_tp_ids, qty=total_qty)
        if old_tp_ids:
            await self._cancel_orders_safe(coin, old_tp_ids)

        # Следующий DCA
        allow_dca = await self.settings.get("allow_dca", "False") == "True"
        next_dca_ids: List[str] = []
        if allow_dca and next_step < config.DCA_MAX_STEPS:
            next_dev = grid["levels"][next_step]
            next_vol = grid["volumes"][next_step + 1]
            next_p = self._round_value(filled_p * (1 - next_dev / 100.0), price_step)
            next_q = self._calc_qty(deposit, next_vol, leverage, next_p, qty_step)
            next_dca_ids = await self._place_limit_chunked(coin, "Buy", next_q, next_p)

        # Все DCA-ордера предыдущих шагов уже FILLED (помечены в monitor). Активные DCA
        # (если были — для следующего шага) заменяем новыми.
        await self.db.replace_active_orders(
            trade_id, ROLE_DCA, next_dca_ids, step=next_step + 1
        )

        await self.notifier.send(
            f"⚖️ <b>DCA #{next_step}: {coin}</b>\nОбъем: {filled_inv:.2f} USDT\n"
            f"Ср.цена: {updated['avg_p']:.4f}\nНовый TP: {new_tp}"
        )
        await self.load_active_positions()

    async def get_active_open_orders(self) -> dict:
        open_orders = {}
        try:
            res = await self._api_call(self.session.get_open_orders, category="linear", settleCoin="USDT")
            if res.get('retCode') == 0:
                for ord in res['result']['list']:
                    sym = ord['symbol']
                    if sym not in open_orders:
                        open_orders[sym] = {'tp': None, 'dca': None}
                    if ord['side'] == 'Sell':
                        open_orders[sym]['tp'] = float(ord['price'])
                    elif ord['side'] == 'Buy':
                        open_orders[sym]['dca'] = float(ord['price'])
        except Exception as e:
            bot_logger.error(f"EXCHANGE: Ошибка получения открытых ордеров: {e}")
        return open_orders

    async def get_last_price(self, coin: str, default: float = 0.0) -> float:
        try:
            res = await self._api_call(self.session.get_tickers, category="linear", symbol=coin)
            if res.get('retCode') == 0:
                return float(res['result']['list'][0]['lastPrice'])
        except Exception:
            pass
        return default

    async def emergency_close_position(self, coin: str) -> dict:
        try:
            trade = await self.db.get_trading_trade(coin)
            if not trade:
                return {"status": "error", "message": "Активная позиция не найдена в БД"}

            trade_id = trade['id']
            # Отменяем все ACTIVE TP/DCA-ордера сделки
            active = await self.db.get_active_orders(trade_id)
            active_ids = [r['order_id'] for r in active]
            if active_ids:
                await self._cancel_orders_safe(coin, active_ids)
                await self.db.mark_orders_status(active_ids, ORDER_CANCELLED)

            current_price = await self.get_last_price(coin)

            pos_res = await self._api_call(self.session.get_positions, category="linear", symbol=coin)
            if pos_res.get('retCode') == 0 and pos_res.get('result', {}).get('list'):
                size = float(pos_res['result']['list'][0]['size'])
                if size > 0:
                    # MARKET закрытие с разбивкой
                    close_ids = await self._place_market_chunked(
                        coin, "Sell", size, reduce_only=True
                    )

                    if close_ids:
                        await self.db.add_orders(trade_id, ROLE_TP, close_ids, qty=size)
                        await self.db.mark_orders_status(close_ids, ORDER_FILLED)

                        # Даём бирже зафиксировать закрытие, затем берём реальный closedPnl
                        await asyncio.sleep(config.EXEC_DATA_DELAY)
                        gross, total_fee, net_closed, last_exit = await self._fetch_realized_pnl(
                            coin, trade["created_at"]
                        )
                        funding_fee = await self._fetch_funding_fee(coin, trade["created_at"])
                        # closedPnl уже включает комиссии и фандинг — финальный net
                        net = net_closed
                        exit_price = last_exit or current_price

                        await self.db.close_trade_realized(coin, exit_price, gross, net, funding_fee)
                        await self.load_active_positions()
                        bot_logger.info(
                            f"🚨 Экстренное закрытие {coin}: Net PNL: {net:.2f} "
                            f"(gross ${gross:.2f}, fee+funding ${total_fee:.4f}, чанков: {len(close_ids)})"
                        )
                        return {"status": "ok", "message": f"Позиция закрыта. PNL: {net:.2f}"}
                    else:
                        return {"status": "error", "message": "Не удалось выставить ни один чанк market-закрытия"}

            return {"status": "error", "message": "Размер позиции 0 или не удалось получить"}
        except Exception as e:
            bot_logger.error(f"EXCHANGE: Ошибка экстренного закрытия {coin}: {e}")
            return {"status": "error", "message": str(e)}