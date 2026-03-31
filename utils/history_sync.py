import asyncio
from telethon import TelegramClient
import config
from parser import parse_signal
from database import Database

async def sync_history(limit=2500):
    db = Database()
    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    print(f"📡 Синхронизация {limit} сообщений из {config.TARGET_CHANNEL}...")
    await client.start()
    count = 0
    async for msg in client.iter_messages(config.TARGET_CHANNEL, limit=limit):
        if not msg.text: continue
        p = parse_signal(msg.text)
        if p:
            p['received_at'], p['raw_text'] = msg.date.isoformat(), msg.text
            if db.save_signal(p): count += 1
    print(f"✅ База наполнена. Добавлено: {count}")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(sync_history(2500))