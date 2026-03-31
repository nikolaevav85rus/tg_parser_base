import config

class PaperExchange:
    def __init__(self, start_balance: float, trades_db=None):
        self.balance = start_balance
        self.active_positions = {}
        self.trades_db = trades_db

    def load_active_positions(self):
        """Восстановление открытых сделок из базы при старте"""
        self.trades_db.cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
        rows = self.trades_db.cursor.fetchall()
        for t in rows:
            step = 0
            if t[9]: step = 3
            elif t[7]: step = 2
            elif t[5]: step = 1
            
            self.active_positions[t[1]] = {
                'avg_price': t[11], 'qty': t[13], 'invested': t[12],
                'target_price': t[11] * 1.007, 'side': 'LONG', 'step': step
            }
        if self.active_positions:
            print(f"📦 Позиций в работе: {len(self.active_positions)}")

    async def execute_signal(self, symbol, s_type, price, target_price):
        if not price: return
        
        # 1. ЛОГИКА ВХОДА И УСРЕДНЕНИЯ
        if s_type == 'OPEN' or 'DCA' in s_type:
            step_num = 0 if s_type == 'OPEN' else int(s_type.split('_')[1])

            if symbol in self.active_positions:
                pos = self.active_positions[symbol]
                # Игнорируем дубли и пройденные шаги
                if s_type == 'OPEN' or step_num <= pos['step']: return

                pct = config.GRID_STEPS[step_num] if step_num < len(config.GRID_STEPS) else 1.0
                amount_usdt = self.balance * (pct / 100)
                qty = amount_usdt / price
                
                pos['invested'] += amount_usdt
                pos['qty'] += qty
                pos['avg_price'] = pos['invested'] / pos['qty']
                pos['step'] = step_num
                
                if self.trades_db:
                    self.trades_db.record_dca(symbol, step_num, price, amount_usdt, pos['avg_price'], pos['invested'], pos['qty'])
                print(f"🔄 [DCA {step_num}] {symbol} | Новая средняя: {pos['avg_price']:.4f}")
            else:
                # Вход по любому сигналу (даже если пропустили OPEN), берем 1%
                amount_usdt = self.balance * (config.GRID_STEPS[0] / 100)
                self.active_positions[symbol] = {
                    'avg_price': price, 'qty': amount_usdt/price, 'invested': amount_usdt,
                    'target_price': target_price or (price * 1.007), 'side': 'LONG', 'step': 0
                }
                if self.trades_db: self.trades_db.record_entry(symbol, price, amount_usdt)
                print(f"🚥 [NEW] {symbol} | Вход по сигналу {s_type} по {price}")

        # 2. ЛОГИКА ЗАКРЫТИЯ (Только если есть открытая сделка)
        elif s_type == 'CLOSE':
            if symbol not in self.active_positions: return
            
            pos = self.active_positions[symbol]
            pnl = (price - pos['avg_price']) * pos['qty']
            pnl_pct = (pnl / pos['invested']) * 100
            self.balance += pnl
            
            if self.trades_db: self.trades_db.record_exit(symbol, price, pnl, pnl_pct)
            self.active_positions.pop(symbol)
            print(f"🏁 [CLOSE] {symbol} | PnL: {pnl:.2f}$ ({pnl_pct:.2f}%)")