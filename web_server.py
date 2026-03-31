from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncio
import config
from logger import bot_logger

app = FastAPI()
templates = Jinja2Templates(directory="templates")

db_i = exchange_i = trades_db_i = settings_db_i = coins_db_i = None

def set_context(d, e, t, s, c):
    global db_i, exchange_i, trades_db_i, settings_db_i, coins_db_i
    db_i, exchange_i, trades_db_i, settings_db_i, coins_db_i = d, e, t, s, c

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})

def _safe_convert(func, value, default):
    if value is None: return default
    try: return func(value)
    except (ValueError, TypeError): return default

@app.get("/api/settings")
async def get_settings():
    if not settings_db_i:
        bot_logger.error("WEB: /api/settings - Контекст БД настроек НЕ установлен!")
        return {}
    try:
        # Собираем данные строго по ключам из твоей БД settings.db
        return {
            "allow_open": settings_db_i.get("allow_open", "False") == "True",
            "allow_dca": settings_db_i.get("allow_dca", "False") == "True",
            "trade_limit": _safe_convert(float, settings_db_i.get("trade_limit"), config.DEPO_USDT),
            "leverage": _safe_convert(int, settings_db_i.get("leverage"), config.LEVERAGE),
            "tp_target": _safe_convert(float, settings_db_i.get("tp_target"), 1.5),
            
            # Объемы (% от депо)
            "dca_0": _safe_convert(float, settings_db_i.get("dca_0"), config.TRADE_PERCENT_1),
            "dca_1": _safe_convert(float, settings_db_i.get("dca_1"), config.TRADE_PERCENT_2),
            "dca_2": _safe_convert(float, settings_db_i.get("dca_2"), config.TRADE_PERCENT_4),
            "dca_3": _safe_convert(float, settings_db_i.get("dca_3"), config.TRADE_PERCENT_8),
            
            # Уровни отклонения (%)
            "dca_level_1": _safe_convert(float, settings_db_i.get("dca_level_1"), 3.5),
            "dca_level_2": _safe_convert(float, settings_db_i.get("dca_level_2"), 6.5),
            "dca_level_3": _safe_convert(float, settings_db_i.get("dca_level_3"), 14.5)
        }
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/settings: {e}")
        return {}

@app.post("/api/settings")
async def update_settings(req: Request):
    data = await req.json()
    if settings_db_i:
        for key, value in data.items():
            settings_db_i.set(key, str(value))
        if exchange_i and "trade_limit" in data:
            exchange_i.update_limit(float(data["trade_limit"]))
    return {"status": "ok"}

@app.get("/api/coins")
async def get_coins():
    if not coins_db_i: return []
    try:
        coins_db_i.cursor.execute("SELECT coin, alias, is_active FROM coins")
        rows = coins_db_i.cursor.fetchall()
        return [{"coin": r[0], "alias": r[1], "is_active": bool(r[2])} for r in rows]
    except Exception as e:
        bot_logger.error(f"WEB: Ошибка в /api/coins: {e}")
        return []

@app.post("/api/coins")
async def add_coin(req: Request):
    data = await req.json()
    if coins_db_i and "coin" in data:
        coins_db_i.add_coin(data["coin"].upper(), data.get("alias", ""), 1)
    return {"status": "ok"}
    
@app.delete("/api/coins/{coin}")
async def delete_coin(coin: str):
    if coins_db_i:
        coins_db_i.cursor.execute("DELETE FROM coins WHERE coin=?", (coin.upper(),))
        coins_db_i.conn.commit()
    return {"status": "ok"}

@app.put("/api/coins/{coin}")
async def update_coin(coin: str, req: Request):
    data = await req.json()
    if coins_db_i and "is_active" in data:
        is_active = 1 if data["is_active"] else 0
        coins_db_i.cursor.execute("UPDATE coins SET is_active = ? WHERE coin = ?", (is_active, coin.upper()))
        coins_db_i.conn.commit()
    return {"status": "ok"}

@app.get("/api/data")
async def get_data():
    if not all([exchange_i, trades_db_i, settings_db_i, db_i]): return {}
    asyncio.create_task(exchange_i.fetch_live_stats())
    eq = exchange_i.get_real_equity()
    limit = settings_db_i.get("trade_limit", config.DEPO_USDT)
    raw_sigs = db_i.cursor.execute("SELECT signal_type, coin, price, received_at FROM signals ORDER BY id DESC LIMIT 50").fetchall()
    sigs = [{"type": s[0], "coin": s[1], "price": s[2], "time": s[3]} for s in raw_sigs]
    positions_data = {}
    for coin, p in exchange_i.active_positions.items():
        live = exchange_i.live_stats.get(coin, {"unrealisedPnl": 0.0})
        gross = live["unrealisedPnl"]
        open_fee = p.get("open_fee", 0.0)
        positions_data[coin] = {
            "step": p["step"], "invested": p["invested"], "avg_price": p["avg_price"], 
            "target_price": p["target_price"], "open_fee": open_fee, "gross_pnl": gross, "net_pnl": gross - open_fee
        }
    return {"equity": eq, "settings": {"limit": float(limit)}, "positions": positions_data, "recent_signals": sigs}

@app.get("/api/history")
async def get_history():
    if not trades_db_i: return {"history": [], "chart": []}
    raw = trades_db_i.get_closed_trades()
    hist, chart, total_net_pnl = [], [], 0
    for t in raw:
        gross = t['pnl'] or 0
        net = t['net_pnl'] if t['net_pnl'] is not None else gross
        total_net_pnl += net
        hist.append({
            "time": t['created_at'], "symbol": t['coin'], "buy_p": t['buy_p'], "avg": t['avg_p'], 
            "exit": t['exit_p'], "total_inv": t['total_inv'], "gross_pnl": round(gross, 2), 
            "pnl_p": round(t['pnl_p'] or 0, 2), "open_fee": round(t['open_fee'] or 0, 4),
            "fund_fee": round(t['funding_fee'] or 0, 4), "close_fee": round(t['close_fee'] or 0, 4), "net_pnl": round(net, 2)
        })
        chart.append({"time": t['created_at'], "total": round(total_net_pnl, 2)})
    return {"history": hist[::-1], "chart": chart}