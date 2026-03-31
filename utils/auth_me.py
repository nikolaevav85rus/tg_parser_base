import asyncio
from telethon import TelegramClient
import config
import os

async def main():
    print("--- 🛡️ СКРИПТ АВТОРИЗАЦИИ 🛡️ ---")
    
    # Путь к файлу сессии
    session_path = f"{config.SESSION_NAME}.session"
    
    # Если старый файл сессии мешает - удаляем его для чистого входа
    if os.path.exists(session_path):
        print(f"⚠️ Старая сессия {session_path} найдена. Удаляю для чистого входа...")
        os.remove(session_path)

    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)

    print("📡 Подключение к серверам Telegram...")
    await client.connect()

    if not await client.is_user_authorized():
        print("❌ Сессия не авторизована.")
        # Вводим номер телефона (например, +79991234567)
        phone = input("📱 Введите ваш номер телефона (в международном формате): ")
        try:
            await client.send_code_request(phone)
            print(f"✅ Код отправлен на номер {phone}")
            print("☝️ Проверь приложение Telegram или дождись SMS.")
            
            code = input("🔢 Введите полученный код: ")
            await client.sign_in(phone, code)
            print("🎉 АВТОРИЗАЦИЯ УСПЕШНА!")
        except Exception as e:
            print(f"🧨 Ошибка при входе: {e}")
    else:
        print("✅ Вы уже авторизованы! Файл сессии актуален.")

    await client.disconnect()
    print("\n[!] Теперь можно закрывать этот скрипт и запускать main.py")

if __name__ == "__main__":
    asyncio.run(main())