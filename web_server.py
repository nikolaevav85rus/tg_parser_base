import asyncio
import aiosqlite
from typing import Any, Dict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import config
from logger import bot_logger

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Глобальные переменные контекста
db_i = None
exchange_i = None
trades_db_i = None
settings_db_i = None
coins_db_i = None

def set_context(d: Any, e: Any, t: Any, s: Any, c: Any) -> None:
    """Установка контекста баз данных и биржи для использования в роутах."""
    global db_i, exchange_i, trades_db_i, settings_db_i, coins_db_i
    db_i, exchange_i, trades_db_i, settings_db_i, coins_db_i = d, e, t, s, c


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


def _safe_convert(func: Any, value: Any, default: Any) -> Any:
    """Безопасная конвертация типов."""
    if value is None: 
        return default
    try: 
        return func(value)
    except (ValueError, TypeError): 
        return default


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    if not settings_db_i:
        return {}
    try:
        return {
            "allow_open": await settings_db_i.get("allow_open", "False") == "True",
            "allow_dca": await settings_db_i.get("allow_dca", "False") == "True",
            "trade_limit": _safe_convert(float, await settings_db_i.get("trade_limit"), getattr(config, 'DEPO_USDT', 100.0)),
            "leverage": _safe_convert(int, await settings_db_i.get("leverage"), getattr(config, 'LEVERAGE', 10)),
            "tp_target": _safe_convert(float, await settings_db_i.get("tp_target"), 1.5),
            
            "dca_0": _safe_convert(float, await settings_db_i.get("dca_0"), getattr(config, 'TRADE_PERCENT_1', 2.0)),
            "dca_1": _safe_convert(float, await settings_db_i.get("dca_1"), getattr(config, 'TRADE_PERCENT_2', 4.0)),
            "dca_2": _safe_convert(float, await settings_db_i.get("dca_2"), getattr(config, 'TRADE_PERCENT_4', 8.0)),
            "dca_3": _safe_convert(float, await settings_db_i.get("dca_3"), getattr(config, 'TRADE_PERCENT_8', 16.0)),
            
            "dca_level_1": _safe_convert(float, await settings_db_i.get("dca_level_1"), 3.5),
            "dca_level_2": _safe_convert(float, await settings_db_i.get("dca_level_2"), 6.5),
            "dca_level_3": _safe_convert(float, await settings_db_i.get("dca_level_3"), 14.5)
        }
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/settings: {e}")
        return {}


@app.post("/api/settings")
async def update_settings(req: Request):
    data = await req.json()
    if settings_db_i:
        for key, value in data.items():
            await settings_db_i.set(key, str(value))
            
        if exchange_i and "trade_limit" in data:
            await exchange_i.update_limit(float(data["trade_limit"]))
    return {"status": "ok"}


@app.get("/api/coins")
async def get_coins():
    if not coins_db_i: 
        return []
    try:
        async with aiosqlite.connect(coins_db_i.db_name) as db:
            cursor = await db.execute("SELECT coin, alias, is_active FROM coins")
            rows = await cursor.fetchall()
            return [{"coin": r[0], "alias": r[1], "is_active": bool(r[2])} for r in rows]
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/coins: {e}")
        return []


@app.post("/api/coins")
async def add_coin(req: Request):
    data = await req.json()
    if coins_db_i and "coin" in data:
        await coins_db_i.add_coin(data["coin"].upper(), data.get("alias", ""), 1)
    return {"status": "ok"}


@app.delete("/api/coins/{coin}")
async def delete_coin(coin: str):
    if coins_db_i:
        async with aiosqlite.connect(coins_db_i.db_name) as db:
            await db.execute("DELETE FROM coins WHERE coin=?", (coin.upper(),))
            await db.commit()
    return {"status": "ok"}


@app.put("/api/coins/{coin}")
async def update_coin(coin: str, req: Request):
    data = await req.json()
    if coins_db_i and "is_active" in data:
        is_active = 1 if data["is_active"] else 0
        async with aiosqlite.connect(coins_db_i.db_name) as db:
            await db.execute("UPDATE coins SET is_active = ? WHERE coin = ?", (is_active, coin.upper()))
            await db.commit()
    return {"status": "ok"}


@app.get("/api/data")
async def get_data():
    if not all([exchange_i, trades_db_i, settings_db_i, db_i]): 
        return {}
    
    limit = await settings_db_i.get("trade_limit", getattr(config, 'DEPO_USDT', 100.0))
    
    # ПАРАЛЛЕЛЬНЫЙ ЗАПРОС с получением Доступного Баланса
    eq_task = asyncio.create_task(exchange_i.get_balance_info())
    orders_task = asyncio.create_task(exchange_i.get_active_open_orders())
    stats_task = asyncio.create_task(exchange_i.fetch_live_stats())
    
    await asyncio.gather(eq_task, orders_task, stats_task)
    
    eq, available_balance = eq_task.result()
    open_orders = orders_task.result()

    sigs = []
    try:
        async with aiosqlite.connect(db_i.db_name) as db:
            cursor = await db.execute("SELECT signal_type, coin, price, received_at FROM signals ORDER BY id DESC LIMIT 50")
            raw_sigs = await cursor.fetchall()
            sigs = [{"type": s[0], "coin": s[1], "price": s[2], "time": s[3]} for s in raw_sigs]
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка при загрузке сигналов: {e}")

    positions_data = {}
    for coin, p in exchange_i.active_positions.items():
        live = exchange_i.live_stats.get(coin, {})
        gross = float(live.get("unrealisedPnl", 0.0))
        
        open_fee = p.get("open_fee", 0.0)
        funding_fee = p.get("funding_fee", 0.0)
        
        current_price = float(live.get("markPrice", p["avg_price"]))

        trade = await trades_db_i.get_trading_trade(coin)
        trade_dict = dict(trade) if trade else {}
        
        actual_tp = open_orders.get(coin, {}).get('tp')
        actual_dca = open_orders.get(coin, {}).get('dca')

        target_p = actual_tp or p.get("target_price") or trade_dict.get("target_p", 0.0)
        
        tp_est_pnl = 0.0
        if target_p and p["avg_price"] > 0:
            est_gross = p["invested"] * (target_p / p["avg_price"] - 1)
            tp_est_pnl = est_gross - open_fee - funding_fee - open_fee 
            
        step = p["step"]
        dca_info = []
        for i in range(1, 4):
            if i <= step:
                price = trade_dict.get(f"dca{i}_p") or 0.0
                status, status_code = "Исполнено", 2
            elif i == step + 1:
                price = actual_dca
                if price:
                    status, status_code = "Ордер (В стакане)", 1
                else:
                    status, status_code = "Ожидание", 0
            else:
                price = None
                status, status_code = "Нет", -1
                
            dca_info.append({
                "level": i, "price": price, "status": status, "status_code": status_code
            })

        positions_data[coin] = {
            "step": step, 
            "invested": p["invested"], 
            "avg_price": p["avg_price"], 
            "current_price": current_price,
            "target_price": target_p, 
            "tp_est_pnl": tp_est_pnl,
            "open_fee": open_fee, 
            "funding": funding_fee, 
            "gross_pnl": gross, 
            "net_pnl": gross - open_fee - funding_fee,
            "dca": dca_info
        }
        
    return {
        "equity": eq, 
        "available_balance": available_balance,
        "settings": {"limit": float(limit)}, 
        "positions": positions_data, 
        "recent_signals": sigs
    }


@app.get("/api/history")
async def get_history():
    if not trades_db_i: 
        return {"history": [], "chart": [], "stats": {}}
        
    raw = await trades_db_i.get_closed_trades()
    hist, chart = [], []
    total_net_pnl = 0.0
    
    stats = {"1d": 0.0, "7d": 0.0, "30d": 0.0, "365d": 0.0, "total": 0.0}
    now = datetime.now(timezone.utc)
    
    for t in raw:
        gross = t['pnl'] or 0.0
        net = t['net_pnl'] if t['net_pnl'] is not None else gross
        total_net_pnl += net
        stats["total"] += net
        
        try:
            created_str = t['created_at'].replace('Z', '+00:00')
            dt = datetime.fromisoformat(created_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
                
            delta = now - dt
            if delta <= timedelta(days=1): stats["1d"] += net
            if delta <= timedelta(days=7): stats["7d"] += net
            if delta <= timedelta(days=30): stats["30d"] += net
            if delta <= timedelta(days=365): stats["365d"] += net
        except Exception:
            pass
        
        hist.append({
            "time": t['created_at'], 
            "symbol": t['coin'], 
            "buy_p": t['buy_p'], 
            "avg": t['avg_p'], 
            "exit": t['exit_p'], 
            "total_inv": t['total_inv'], 
            "gross_pnl": round(gross, 2), 
            "pnl_p": round(t['pnl_p'] or 0, 2), 
            "open_fee": round(t['open_fee'] or 0.0, 4),
            "fund_fee": round(t['funding_fee'] or 0.0, 4), 
            "close_fee": round(t['close_fee'] or 0.0, 4), 
            "net_pnl": round(net, 2)
        })
        chart.append({"time": t['created_at'], "total": round(total_net_pnl, 2)})
        
    for k in stats:
        stats[k] = round(stats[k], 2)
        
    return {"history": hist[::-1], "chart": chart, "stats": stats}


@app.post("/api/positions/{coin}/close")
async def close_position_manual(coin: str):
    """Экстренное закрытие позиции по рынку (Вызывает инкапсулированный метод биржи)."""
    if not exchange_i:
        return {"status": "error", "message": "Контекст биржи не инициализирован"}
    
    return await exchange_i.emergency_close_position(coin)