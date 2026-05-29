"""
Пересчёт net_pnl закрытых сделок по РЕАЛЬНЫМ данным биржи (Σ closedPnl).

Старые сделки с DCA могли получить заниженный net_pnl из-за самостоятельного
расчёта средней цены, который расходился с биржей при частичном исполнении TP
до усреднения. Этот скрипт берёт сумму closedPnl с биржи за период жизни сделки
и пересчитывает net_pnl = Σ closedPnl.

ВАЖНО: closedPnl Bybit УЖЕ включает все комиссии И фандинг — это финальный net.
Фандинг отдельно НЕ вычитается (иначе двойной учёт).

Окно сделки: [created_at .. closed_at + буфер], но обрезается по времени
следующей сделки того же символа — чтобы не захватить чужие закрытия.

Запуск из корня проекта:
    python utils/backfill_realized_pnl.py            # dry-run
    python utils/backfill_realized_pnl.py --apply    # применить

Перед --apply: остановить бота.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from pybit.unified_trading import HTTP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import config
from database.base import DB_DIR


DB_PATH = os.path.join(DB_DIR, "trades.db")
END_BUFFER_MS = 120_000   # +2 мин на запись closed_at в БД после реального закрытия
START_BUFFER_MS = 5_000   # −5 сек на возможный лаг created_at


def _iso_to_ms(iso_s: Optional[str]) -> Optional[int]:
    if not iso_s:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


async def fetch_closed_pnl_sum(
    session: HTTP, symbol: str, start_ms: int, end_ms: int
) -> Tuple[float, int]:
    """Σ closedPnl по символу за [start_ms..end_ms]. Возвращает (sum, кол-во записей)."""
    if end_ms <= start_ms:
        return 0.0, 0
    total = 0.0
    count = 0
    cursor: Optional[str] = None
    for _ in range(10):
        params: Dict[str, Any] = {
            "category": "linear", "symbol": symbol,
            "startTime": start_ms, "endTime": end_ms, "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        res = await asyncio.to_thread(session.get_closed_pnl, **params)
        if res.get('retCode') != 0:
            print(f"    [WARN] API: {res.get('retMsg')} (код {res.get('retCode')})")
            break
        result = res.get('result', {}) or {}
        items = result.get('list', []) or []
        for it in items:
            ts = int(it.get('updatedTime', 0) or 0)
            if start_ms <= ts <= end_ms:
                try:
                    total += float(it.get('closedPnl', 0) or 0)
                    count += 1
                except (TypeError, ValueError):
                    continue
        cursor = result.get('nextPageCursor') or None
        if not cursor or len(items) < 100:
            break
    return total, count


async def backfill(apply: bool = False) -> None:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] БД не найдена: {DB_PATH}")
        return

    session = HTTP(
        testnet=config.BYBIT_TESTNET,
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, coin, step, created_at, closed_at, net_pnl, funding_fee, pnl "
            "FROM trades WHERE status='closed' ORDER BY id ASC"
        )
        rows = [dict(r) for r in await cur.fetchall()]

    print(f"Найдено {len(rows)} закрытых сделок\n")

    # Для обрезки окна по следующей сделке того же символа:
    # next_created_ms[coin] = created_ms следующей по времени сделки этого символа
    next_open_by_coin: Dict[str, List[int]] = {}
    for r in rows:
        cms = _iso_to_ms(r['created_at'])
        if cms is not None:
            next_open_by_coin.setdefault(r['coin'], []).append(cms)
    for coin in next_open_by_coin:
        next_open_by_coin[coin].sort()

    def next_open_after(coin: str, ts: int) -> Optional[int]:
        for c in next_open_by_coin.get(coin, []):
            if c > ts:
                return c
        return None

    print(f"  {'ID':>4} {'Монета':>11} {'st':>2} | {'net_old':>10} {'Σclosed':>10} {'fund':>8} {'net_new':>10}  delta   recs")
    print("  " + "-" * 86)

    to_update: List[Tuple[float, float, int]] = []
    total_delta = 0.0

    for r in rows:
        coin = r['coin']
        start_ms = _iso_to_ms(r['created_at'])
        closed_ms = _iso_to_ms(r['closed_at'] or r['created_at'])
        if start_ms is None or closed_ms is None:
            continue

        win_start = start_ms - START_BUFFER_MS
        win_end = closed_ms + END_BUFFER_MS
        # обрезаем по следующей сделке того же символа
        nxt = next_open_after(coin, start_ms)
        if nxt is not None:
            win_end = min(win_end, nxt - 1000)

        sum_closed, recs = await fetch_closed_pnl_sum(session, coin, win_start, win_end)
        funding = float(r['funding_fee'] or 0.0)
        # closedPnl уже финальный net (с комиссиями и фандингом) — НЕ вычитаем funding
        net_new = sum_closed
        net_old = float(r['net_pnl'] or 0.0)
        delta = net_new - net_old

        changed = abs(delta) >= 0.01 and recs > 0
        mark = " " if changed else "="
        print(
            f"  {r['id']:>4} {coin:>11} {r['step']:>2} | {net_old:>10.2f} {sum_closed:>10.2f} "
            f"{funding:>8.4f} {net_new:>10.2f} {delta:+8.2f} {recs:>4} {mark}"
        )

        if changed:
            to_update.append((net_new, sum_closed, r['id']))
            total_delta += delta

        await asyncio.sleep(0.1)

    print()
    print(f"  Σ суммарная поправка к Total Net PNL: {total_delta:+.2f} USDT")
    print(f"  Требуют обновления: {len(to_update)} сделок\n")

    if not apply:
        print("DRY-RUN. Для применения: python utils/backfill_realized_pnl.py --apply")
        return

    if not to_update:
        print("Нечего обновлять.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        for net_new, sum_closed, trade_id in to_update:
            # pnl (gross в БД) оставляем как было — биржевой closedPnl уже net.
            # Обновляем только net_pnl как источник истины для статистики.
            await db.execute(
                "UPDATE trades SET net_pnl=? WHERE id=?", (net_new, trade_id)
            )
        await db.commit()

    print(f"[OK] Обновлено {len(to_update)} сделок.")


if __name__ == "__main__":
    asyncio.run(backfill(apply="--apply" in sys.argv))
