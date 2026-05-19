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


def _init_empty_context() -> None:
    """Инициализация пустого web-контекста для прямого импорта FastAPI app."""
    app.state.db = None
    app.state.exchange = None
    app.state.trades_db = None
    app.state.settings_db = None
    app.state.coins = None


def set_context(d: Any, e: Any, t: Any, s: Any, c: Any) -> None:
    """Установка контекста баз данных и биржи через app.state."""
    app.state.db = d
    app.state.exchange = e
    app.state.trades_db = t
    app.state.settings_db = s
    app.state.coins = c


_init_empty_context()


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


def _parse_trade_datetime(value: Any) -> datetime | None:
    """Парсинг даты сделки из ISO-строки с безопасным fallback."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    if not app.state.settings_db:
        return {}
    try:
        return {
            "allow_open": await app.state.settings_db.get("allow_open", "False") == "True",
            "allow_dca": await app.state.settings_db.get("allow_dca", "False") == "True",
            "trade_limit": _safe_convert(float, await app.state.settings_db.get("trade_limit"), getattr(config, 'DEPO_USDT', 100.0)),
            "leverage": _safe_convert(int, await app.state.settings_db.get("leverage"), getattr(config, 'LEVERAGE', 10)),
            "tp_target": _safe_convert(float, await app.state.settings_db.get("tp_target"), 1.5),
            
            # --- ДОБАВЛЕН ЛИМИТ АКТИВНЫХ СДЕЛОК ---
            "max_active_trades": _safe_convert(int, await app.state.settings_db.get("max_active_trades"), 3),
            
            "dca_0": _safe_convert(float, await app.state.settings_db.get("dca_0"), getattr(config, 'TRADE_PERCENT_1', 2.0)),
            "dca_1": _safe_convert(float, await app.state.settings_db.get("dca_1"), getattr(config, 'TRADE_PERCENT_2', 4.0)),
            "dca_2": _safe_convert(float, await app.state.settings_db.get("dca_2"), getattr(config, 'TRADE_PERCENT_4', 8.0)),
            "dca_3": _safe_convert(float, await app.state.settings_db.get("dca_3"), getattr(config, 'TRADE_PERCENT_8', 16.0)),
            
            "dca_level_1": _safe_convert(float, await app.state.settings_db.get("dca_level_1"), 3.5),
            "dca_level_2": _safe_convert(float, await app.state.settings_db.get("dca_level_2"), 6.5),
            "dca_level_3": _safe_convert(float, await app.state.settings_db.get("dca_level_3"), 14.5)
        }
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/settings: {e}")
        return {}


@app.post("/api/settings")
async def update_settings(req: Request):
    data = await req.json()
    if app.state.settings_db:
        for key, value in data.items():
            await app.state.settings_db.set(key, str(value))
            
        if app.state.exchange and "trade_limit" in data:
            await app.state.exchange.update_limit(float(data["trade_limit"]))
    return {"status": "ok"}


@app.get("/api/coins")
async def get_coins():
    if not app.state.coins:
        return []
    try:
        return await app.state.coins.get_all()
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/coins: {e}")
        return []


@app.post("/api/coins")
async def add_coin(req: Request):
    data = await req.json()
    if app.state.coins and "coin" in data:
        await app.state.coins.add_coin(data["coin"].upper(), data.get("alias", ""), 1)
    return {"status": "ok"}


@app.delete("/api/coins/{coin}")
async def delete_coin(coin: str):
    if app.state.coins:
        await app.state.coins.delete(coin)
    return {"status": "ok"}


@app.put("/api/coins/{coin}")
async def update_coin(coin: str, req: Request):
    data = await req.json()
    if app.state.coins and "is_active" in data:
        await app.state.coins.set_active(coin, bool(data["is_active"]))
    return {"status": "ok"}


@app.get("/api/data")
async def get_data():
    if not all([app.state.exchange, app.state.trades_db, app.state.settings_db, app.state.db]): 
        return {}
    
    limit = await app.state.settings_db.get("trade_limit", getattr(config, 'DEPO_USDT', 100.0))
    
    # ПАРАЛЛЕЛЬНЫЙ ЗАПРОС с получением Доступного Баланса
    eq_task = asyncio.create_task(app.state.exchange.get_balance_info())
    orders_task = asyncio.create_task(app.state.exchange.get_active_open_orders())
    stats_task = asyncio.create_task(app.state.exchange.fetch_live_stats())
    
    await asyncio.gather(eq_task, orders_task, stats_task)
    
    eq, available_balance = eq_task.result()
    open_orders = orders_task.result()

    sigs = []
    try:
        async with aiosqlite.connect(app.state.db.db_name) as db:
            cursor = await db.execute(f"SELECT signal_type, coin, price, received_at FROM signals ORDER BY id DESC LIMIT {config.SIGNALS_LIMIT}")
            raw_sigs = await cursor.fetchall()
            sigs = [{"type": s[0], "coin": s[1], "price": s[2], "time": s[3]} for s in raw_sigs]
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка при загрузке сигналов: {e}")

    all_trades = await app.state.trades_db.get_open_trades()
    trades_by_coin = {t['coin']: dict(t) for t in all_trades}

    positions_data = {}
    for coin, p in app.state.exchange.active_positions.items():
        live = app.state.exchange.live_stats.get(coin, {})
        gross = float(live.get("unrealisedPnl", 0.0))

        open_fee = p.get("open_fee", 0.0)
        funding_fee = p.get("funding_fee", 0.0)

        current_price = float(live.get("markPrice", p["avg_price"]))

        trade_dict = trades_by_coin.get(coin, {})
        
        actual_tp = open_orders.get(coin, {}).get('tp')
        actual_dca = open_orders.get(coin, {}).get('dca')

        target_p = actual_tp or p.get("target_price") or trade_dict.get("target_p", 0.0)
        
        tp_est_pnl = 0.0
        if target_p and p["avg_price"] > 0:
            est_gross = p["invested"] * (target_p / p["avg_price"] - 1)
            tp_est_pnl = est_gross - open_fee - funding_fee
            
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
    if not app.state.trades_db: 
        return {"history": [], "chart": [], "stats": {}}
        
    raw = await app.state.trades_db.get_closed_trades()
    hist, chart = [], []
    total_net_pnl = 0.0
    
    stats = {"1d": 0.0, "7d": 0.0, "30d": 0.0, "365d": 0.0, "total": 0.0}
    now = datetime.now(timezone.utc)
    
    for t in raw:
        gross = t['pnl'] or 0.0
        net = t['net_pnl'] if t['net_pnl'] is not None else gross
        total_net_pnl += net
        stats["total"] += net
        closed_at_raw = t['closed_at'] or t['created_at']
        closed_dt = _parse_trade_datetime(closed_at_raw)
        
        if closed_dt:
            delta = now - closed_dt
            if delta <= timedelta(days=1): stats["1d"] += net
            if delta <= timedelta(days=7): stats["7d"] += net
            if delta <= timedelta(days=30): stats["30d"] += net
            if delta <= timedelta(days=365): stats["365d"] += net
        
        hist.append({
            "time": closed_at_raw,
            "opened_at": t['created_at'],
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
        chart.append({"time": closed_at_raw, "total": round(total_net_pnl, 2)})
        
    for k in stats:
        stats[k] = round(stats[k], 2)
        
    return {"history": hist[::-1], "chart": chart, "stats": stats}


@app.post("/api/positions/{coin}/close")
async def close_position_manual(coin: str):
    """Экстренное закрытие позиции по рынку (Вызывает инкапсулированный метод биржи)."""
    if not app.state.exchange:
        return {"status": "error", "message": "Контекст биржи не инициализирован"}
    
    return await app.state.exchange.emergency_close_position(coin)
