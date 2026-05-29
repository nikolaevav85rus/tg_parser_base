"""
Сводка лимитов биржи Bybit по всем активным монетам в whitelist.

Сопоставляет maxMktOrderQty / maxOrderQty с текущими настройками бота
(trade_limit, leverage, dca_0/1/2/3) и показывает, какие шаги стратегии
не помещаются в лимиты по каждой монете.

Запуск из корня проекта:
    python utils/check_limits.py
"""
import asyncio
import math
import os
import sys
from typing import Dict, Any, List, Optional

import aiosqlite
from pybit.unified_trading import HTTP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import config
from database.base import DB_DIR


# ---------------------------------------------------------------------------
# Чистая копия алгоритма _split_qty из bybit_exchange.py — для офлайн-симуляции.
# Должна совпадать с тем, что делает бот в проде.
# ---------------------------------------------------------------------------
def _round_value(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision = int(abs(math.log10(step))) if step < 1 else 0
    return round(math.floor(value / step) * step, precision)


def split_qty(total: float, max_per_order: float, step: float, min_qty: float = 0.0) -> List[float]:
    total = _round_value(total, step)
    if total <= 0:
        return []
    if max_per_order <= 0 or total <= max_per_order:
        return [total]
    n = math.ceil(total / max_per_order)
    base = _round_value(total / n, step)
    if base <= 0:
        return [total]
    if base > max_per_order:
        base = _round_value(max_per_order, step)
        n = math.ceil(total / base) if base > 0 else 1
    chunks: List[float] = [base] * (n - 1)
    last = _round_value(total - base * (n - 1), step)
    if last <= 0:
        return chunks
    if min_qty and chunks and last < min_qty:
        merged = _round_value(chunks[-1] + last, step)
        if merged <= max_per_order:
            chunks[-1] = merged
            return chunks
    chunks.append(last)
    return chunks


COINS_DB = os.path.join(DB_DIR, "coins.db")
SETTINGS_DB = os.path.join(DB_DIR, "settings.db")


async def load_active_coins() -> List[str]:
    async with aiosqlite.connect(COINS_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT coin, alias FROM coins WHERE is_active=1 ORDER BY coin"
        )
        rows = await cur.fetchall()
        # alias имеет приоритет над coin — это то, чем бот реально торгует
        return [r['alias'] or r['coin'] for r in rows]


async def load_settings() -> Dict[str, str]:
    async with aiosqlite.connect(SETTINGS_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
        return {r['key']: r['value'] for r in rows}


async def fetch_instrument(session: HTTP, coin: str) -> Optional[Dict[str, Any]]:
    res = await asyncio.to_thread(
        session.get_instruments_info, category="linear", symbol=coin
    )
    if res.get('retCode') != 0:
        return None
    lst = res.get('result', {}).get('list', [])
    if not lst:
        return None
    info = lst[0]
    lot = info.get('lotSizeFilter', {}) or {}
    return {
        "maxOrderQty":    float(lot.get('maxOrderQty', 0) or 0),
        "maxMktOrderQty": float(lot.get('maxMktOrderQty', lot.get('maxOrderQty', 0)) or 0),
        "minOrderQty":    float(lot.get('minOrderQty', 0) or 0),
        "qtyStep":        float(lot.get('qtyStep', 0.001) or 0.001),
    }


async def fetch_price(session: HTTP, coin: str) -> float:
    res = await asyncio.to_thread(
        session.get_tickers, category="linear", symbol=coin
    )
    if res.get('retCode') != 0:
        return 0.0
    lst = res.get('result', {}).get('list', [])
    if not lst:
        return 0.0
    try:
        return float(lst[0].get('lastPrice', 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def calc_qty(deposit: float, percent: float, leverage: int, price: float) -> float:
    if price <= 0:
        return 0.0
    return (deposit * (percent / 100.0) * leverage) / price


async def main() -> None:
    simulate = "--simulate" in sys.argv

    coins = await load_active_coins()
    settings = await load_settings()

    trade_limit = float(settings.get('trade_limit', str(config.DEPO_USDT)))
    leverage = int(float(settings.get('leverage', str(config.LEVERAGE))))
    dca_pcts = [
        float(settings.get('dca_0', '2')),
        float(settings.get('dca_1', '4')),
        float(settings.get('dca_2', '8')),
        float(settings.get('dca_3', '16')),
    ]

    print(f"Настройки бота:")
    print(f"  trade_limit = ${trade_limit:.0f}")
    print(f"  leverage    = {leverage}x")
    print(f"  dca % шагов = {dca_pcts[0]} / {dca_pcts[1]} / {dca_pcts[2]} / {dca_pcts[3]}")
    print(f"  активных монет: {len(coins)}\n")

    session = HTTP(
        testnet=config.BYBIT_TESTNET,
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
    )

    # Заголовок таблицы
    header = (
        f"  {'Coin':<11} {'Price':>10} | "
        f"{'maxMkt ($)':>12} {'maxLim ($)':>12} | "
        f"{'qty_0':>10} {'M0':>3}  "
        f"{'qty_1':>10} {'L1':>3}  "
        f"{'qty_2':>10} {'L2':>3}  "
        f"{'qty_3':>10} {'L3':>3}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    problems: List[str] = []

    for coin in coins:
        info = await fetch_instrument(session, coin)
        if info is None:
            print(f"  {coin:<11} [нет данных по инструменту]")
            continue

        price = await fetch_price(session, coin)
        if price <= 0:
            print(f"  {coin:<11} [нет цены]")
            continue

        max_mkt = info['maxMktOrderQty']
        max_lim = info['maxOrderQty']
        max_mkt_usd = max_mkt * price
        max_lim_usd = max_lim * price

        qtys = [calc_qty(trade_limit, p, leverage, price) for p in dca_pcts]

        # M = market (вход), L = limit (DCA)
        fits = [
            qtys[0] <= max_mkt,  # вход — market
            qtys[1] <= max_lim,  # DCA-1 — limit
            qtys[2] <= max_lim,  # DCA-2 — limit
            qtys[3] <= max_lim,  # DCA-3 — limit
        ]

        def mark(ok: bool) -> str:
            return " OK" if ok else "BAD"

        print(
            f"  {coin:<11} {price:>10.5f} | "
            f"{max_mkt_usd:>12,.0f} {max_lim_usd:>12,.0f} | "
            f"{qtys[0]:>10,.0f} {mark(fits[0])}  "
            f"{qtys[1]:>10,.0f} {mark(fits[1])}  "
            f"{qtys[2]:>10,.0f} {mark(fits[2])}  "
            f"{qtys[3]:>10,.0f} {mark(fits[3])}"
        )

        bad_steps = [name for name, ok in zip(["вход", "DCA-1", "DCA-2", "DCA-3"], fits) if not ok]
        if bad_steps:
            problems.append(f"  {coin}: не помещаются — {', '.join(bad_steps)}")

        # --simulate: для проблемных монет показываем разбивку на чанки
        if simulate and bad_steps:
            step_size = info['qtyStep']
            min_qty = info['minOrderQty']
            roles_max = [
                ("вход",  max_mkt),
                ("DCA-1", max_lim),
                ("DCA-2", max_lim),
                ("DCA-3", max_lim),
            ]
            for (label, max_q), q in zip(roles_max, qtys):
                if q <= 0:
                    continue
                chunks = split_qty(q, max_q, step_size, min_qty=min_qty)
                marker = "  " if q <= max_q else "* "
                summed = sum(chunks)
                print(
                    f"    {marker}{label:>6}: qty={q:,.4f} → "
                    f"{len(chunks)} чанк(а/ов) Σ={summed:,.4f}: "
                    f"{['{:,.4f}'.format(c) for c in chunks]}"
                )

        await asyncio.sleep(0.05)

    print()
    if problems:
        print(f"Проблемные монеты ({len(problems)}):")
        for p in problems:
            print(p)
    else:
        print("Все монеты помещаются в лимиты при текущих настройках.")


if __name__ == "__main__":
    asyncio.run(main())
