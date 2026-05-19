"""
Бэкфилл funding_fee и net_pnl для закрытых сделок в db/trades.db.

Для каждой сделки запрашивает get_transaction_log type=SETTLEMENT за период
[created_at..closed_at] по символу, суммирует фандинг, пересчитывает net_pnl
по формуле gross - open_fee - close_fee - funding_fee.

Запуск из корня проекта:
    python utils/backfill_funding.py            # dry-run, ничего не пишет
    python utils/backfill_funding.py --apply    # применить изменения

Перед --apply: остановить бота, чтобы не было гонки за trades.db.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import aiosqlite
from pybit.unified_trading import HTTP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from database.base import DB_DIR


DB_PATH = os.path.join(DB_DIR, "trades.db")


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


async def fetch_funding(session: HTTP, symbol: str, start_ms: int, end_ms: int) -> float:
    """Сумма funding по символу за период [start_ms..end_ms]. Положительное число = расход."""
    if end_ms <= start_ms:
        return 0.0

    total = 0.0
    cursor: Optional[str] = None
    for _ in range(20):
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

        res = await asyncio.to_thread(session.get_transaction_log, **params)
        if res.get('retCode') != 0:
            print(f"    [WARN] API ошибка: {res.get('retMsg')} (Код: {res.get('retCode')})")
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

    return -total


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
            "SELECT id, coin, created_at, closed_at, pnl, open_fee, close_fee, "
            "funding_fee, net_pnl "
            "FROM trades WHERE status='closed' "
            "ORDER BY id ASC"
        )
        rows = await cur.fetchall()

    print(f"Найдено {len(rows)} закрытых сделок\n")

    candidates = []
    skipped_migrated = 0
    skipped_parse = 0
    for r in rows:
        start_ms = _iso_to_ms(r['created_at'])
        end_ms = _iso_to_ms(r['closed_at'] or r['created_at'])
        if start_ms is None or end_ms is None:
            skipped_parse += 1
            continue
        if end_ms <= start_ms:
            skipped_migrated += 1
            continue
        candidates.append((r, start_ms, end_ms))

    print(f"  Пропущено (closed_at == created_at, мигрированные): {skipped_migrated}")
    print(f"  Пропущено (ошибка парсинга дат):                    {skipped_parse}")
    print(f"  Кандидатов для бэкфилла:                            {len(candidates)}\n")

    if not candidates:
        return

    print(f"  {'ID':>4} {'Монета':>10} | {'funding (new)':>14} {'funding (old)':>14} | {'net (old)':>10} -> {'net (new)':>10}   delta")
    print("  " + "-" * 95)

    total_delta = 0.0
    to_update = []
    for r, start_ms, end_ms in candidates:
        coin = r['coin']
        old_funding = float(r['funding_fee'] or 0)
        funding = await fetch_funding(session, coin, start_ms, end_ms)

        gross = float(r['pnl'] or 0)
        open_fee = float(r['open_fee'] or 0)
        close_fee = float(r['close_fee'] or 0)
        new_net = gross - open_fee - close_fee - funding
        old_net = float(r['net_pnl'] or 0)
        delta = new_net - old_net
        total_delta += delta

        changed = abs(funding - old_funding) >= 0.0001
        mark = " " if changed else "="
        print(f"  {r['id']:>4} {coin:>10} | {funding:>14.4f} {old_funding:>14.4f} | {old_net:>10.2f} -> {new_net:>10.2f} {delta:+8.2f} {mark}")

        if changed:
            to_update.append((funding, new_net, r['id']))

        await asyncio.sleep(0.1)  # rate-limit для API

    print("\n  Sum: суммарная поправка к Total Net PNL: {:+.2f} USDT".format(total_delta))
    print(f"  Требуют обновления: {len(to_update)} сделок\n")

    if not apply:
        print("Это DRY-RUN. Для применения: python utils/backfill_funding.py --apply")
        return

    if not to_update:
        print("Нечего обновлять.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        for funding, new_net, trade_id in to_update:
            await db.execute(
                "UPDATE trades SET funding_fee=?, net_pnl=? WHERE id=?",
                (funding, new_net, trade_id)
            )
        await db.commit()

    print(f"[OK] Обновлено {len(to_update)} сделок.")


if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    asyncio.run(backfill(apply=apply_flag))
