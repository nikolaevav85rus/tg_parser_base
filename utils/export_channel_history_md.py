import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv
from telethon import TelegramClient


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
ENV_PATH_CONFIG = CONFIG_DIR / ".env"
ENV_PATH_ROOT = BASE_DIR / ".env"
DEFAULT_OUTPUT = BASE_DIR / "arc" / "channel_history_last_4_years.md"


def _load_env() -> None:
    if ENV_PATH_CONFIG.exists():
        load_dotenv(ENV_PATH_CONFIG)
        return
    if ENV_PATH_ROOT.exists():
        load_dotenv(ENV_PATH_ROOT)
        return
    print("❌ Файл .env не найден ни в config/, ни в корне проекта.")
    sys.exit(1)


def _get_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "")
    return int(value) if value and value.lstrip("-").isdigit() else default


def _get_target_channel() -> Union[int, str]:
    value = os.getenv("TG_TARGET_CHANNEL", "").strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _get_session_name() -> str:
    session_name = os.getenv("SESSION_NAME", "session_settings").strip() or "session_settings"
    return str(CONFIG_DIR / session_name)


def _prepare_session_copy(session_name: str) -> str:
    """
    Создает временную копию Telethon session sqlite-файла, чтобы не конфликтовать
    с уже запущенным ботом, который держит оригинал открытым.
    """
    session_path = Path(f"{session_name}.session")
    if not session_path.exists():
        return session_name

    temp_dir = Path(tempfile.mkdtemp(prefix="tg_parser_session_"))
    temp_session_base = temp_dir / session_path.stem
    temp_session_path = temp_session_base.with_suffix(".session")
    shutil.copy2(session_path, temp_session_path)
    return str(temp_session_base)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Выгрузка истории Telegram-канала в Markdown за последние N лет."
    )
    parser.add_argument(
        "--years",
        type=int,
        default=4,
        help="Сколько последних лет выгружать. По умолчанию: 4",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Путь к выходному .md файлу. По умолчанию: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Переопределить TG_TARGET_CHANNEL из .env",
    )
    return parser.parse_args()


def _normalize_channel(channel_arg: Optional[str], env_channel: Union[int, str]) -> Union[int, str]:
    if channel_arg is None:
        return env_channel
    channel_arg = channel_arg.strip()
    if channel_arg.lstrip("-").isdigit():
        return int(channel_arg)
    return channel_arg


def _format_message_text(text: str) -> str:
    if not text.strip():
        return "_[Сообщение без текста или только медиа]_"
    return f"```\n{text.rstrip()}\n```"


async def export_history(years: int, output_path: Path, channel: Union[int, str]) -> None:
    api_id = _get_int("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH", "").strip()
    session_name = _get_session_name()

    if not api_id:
        print("❌ Не задан TG_API_ID в .env")
        sys.exit(1)
    if not api_hash:
        print("❌ Не задан TG_API_HASH в .env")
        sys.exit(1)
    if not channel:
        print("❌ Не задан TG_TARGET_CHANNEL в .env и не передан --channel")
        sys.exit(1)
    if years <= 0:
        print("❌ Параметр --years должен быть больше 0")
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=365 * years + years // 4)

    temp_session_name = _prepare_session_copy(session_name)
    client = TelegramClient(temp_session_name, api_id, api_hash)
    await client.start()

    try:
        entity = await client.get_entity(channel)
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(channel)
        username = getattr(entity, "username", None)

        print(f"Loading channel history: {title}")
        print(f"Period: {since_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} -> {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        header_lines = [
            f"# История канала: {title}",
            "",
            f"- Канал: `{channel}`",
            f"- Период: с `{since_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}` по `{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}`",
            f"- Сформировано: `{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}`",
            "",
            "---",
            "",
        ]

        output_path.write_text("\n".join(header_lines), encoding="utf-8")

        saved_count = 0
        scanned_count = 0

        with output_path.open("a", encoding="utf-8") as output_file:
            async for message in client.iter_messages(entity):
                scanned_count += 1

                message_date = message.date
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)

                if message_date < since_utc:
                    break

                text = message.message or ""
                if not text and not message.media:
                    continue

                saved_count += 1

                entry_lines = [
                    f"## {message_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | ID {message.id}",
                    "",
                ]

                if username:
                    entry_lines.append(f"- Ссылка: https://t.me/{username}/{message.id}")
                entry_lines.append(f"- Дата: `{message_date.astimezone(timezone.utc).isoformat()}`")
                if message.views is not None:
                    entry_lines.append(f"- Просмотры: `{message.views}`")
                if message.forwards is not None:
                    entry_lines.append(f"- Пересылки: `{message.forwards}`")
                if message.media and not text:
                    entry_lines.append("- Медиа: `yes`")
                entry_lines.extend([
                    "",
                    _format_message_text(text),
                    "",
                ])

                output_file.write("\n".join(entry_lines))
                output_file.flush()

                if saved_count % 100 == 0:
                    print(f"  Saved messages: {saved_count}")

        print("")
        print(f"Done. Messages scanned: {scanned_count}")
        print(f"Saved to Markdown: {saved_count}")
        print(f"File: {output_path}")
    finally:
        await client.disconnect()


async def main() -> None:
    _load_env()
    args = _parse_args()
    channel = _normalize_channel(args.channel, _get_target_channel())
    await export_history(args.years, args.output, channel)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
