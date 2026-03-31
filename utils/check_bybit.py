from pybit.unified_trading import HTTP
import config # Мы потом добавим туда ключи

# Временно вставим ключи прямо сюда для теста
# ВАЖНО: Потом удали их отсюда и перенеси в config.py!
API_KEY = "XkomVPs9U9ZGdUfTzs"
API_SECRET = "nJwC4jvYaiFD3N738q6nOezZjw4qEdJUVnQU"

def test_bybit_connection():
    print("🚀 Тестируем подключение к Bybit...")
    
    try:
        # Инициализация сессии
        session = HTTP(
            testnet=False, # Смени на True, если ключи от тестовой сети
            api_key=API_KEY,
            api_secret=API_SECRET,
        )

        # Запрашиваем баланс единого торгового аккаунта (UTA)
        # Для субаккаунтов это самый верный способ проверить связь
        response = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        
        if response['retCode'] == 0:
            print("✅ Подключение успешно!")
            balance_data = response['result']['list'][0]
            for coin in balance_data['coin']:
                if coin['coin'] == 'USDT':
                    print(f"💰 Доступный баланс: {coin['walletBalance']} USDT")
                    print(f"📈 Всего средств (Equity): {coin['equity']} USDT")
        else:
            print(f"❌ Биржа вернула ошибку: {response['retMsg']}")

    except Exception as e:
        print(f"🧨 Произошла системная ошибка: {e}")

if __name__ == "__main__":
    test_bybit_connection()