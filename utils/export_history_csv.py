import csv
import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
import config
from parser import parse_signal

async def export_to_csv():
    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.start()
    
    print(f"Connecting to channel: {config.TARGET_CHANNEL}...")
    
    with open('historical_signals.csv', mode='w', encoding='utf-8', newline='') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(['Date', 'Coin', 'Signal Type', 'Direction', 'Price', 'Target Price', 'Raw Text'])
        
        count = 0
        valid_count = 0
        
        async for message in client.iter_messages(config.TARGET_CHANNEL):
            if not message.text:
                continue
                
            count += 1
            if count % 100 == 0:
                print(f"Processed messages: {count} | Found signals: {valid_count}")
                
            parsed = parse_signal(message.text)
            
            if parsed:
                valid_count += 1
                clean_text = message.text.replace('\n', ' ').replace('\r', '')
                msg_date = message.date.strftime('%Y-%m-%d %H:%M:%S')
                
                writer.writerow([
                    msg_date,
                    parsed.get('coin', ''),
                    parsed.get('signal_type', ''),
                    parsed.get('direction', ''),
                    parsed.get('price', ''),
                    parsed.get('target_price', ''),
                    clean_text
                ])
                
    print(f"Export completed. Total scanned: {count}. Valid signals saved: {valid_count}.")

if __name__ == '__main__':
    if sys.platform == "win32": 
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(export_to_csv())