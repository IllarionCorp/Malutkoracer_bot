import argparse
import asyncio
import logging
from pathlib import Path
import sqlite3

import psycopg

from telegram.error import TelegramError

from .config import Settings
from .handlers import build_application
from .migration import migrate_sqlite
from .service import BookingService
from .storage import BookingStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот бронирования гоночного трека")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Проверить настройки и PostgreSQL без обращения к Telegram")
    actions.add_argument("--migrate-sqlite", type=Path, metavar="PATH", help="Перенести SQLite в пустую PostgreSQL и выйти")
    args = parser.parse_args()
    try:
        settings = Settings.from_env(require_token=not (args.check or args.migrate_sqlite is not None))
    except ValueError as error:
        parser.exit(1, f"Ошибка настройки: {error}\n")
    store = BookingStore(settings.database_url)
    try:
        if args.migrate_sqlite is not None:
            counts = migrate_sqlite(store, args.migrate_sqlite)
            print(f"Перенос в PostgreSQL завершён. Окон: {counts['availability']}, броней: {counts['bookings']}, "
                  f"настроек трека: {counts['track_info']}. Файл SQLite сохранён.")
            return
        store.initialize()
    except psycopg.Error as error:
        parser.exit(1, f"Ошибка PostgreSQL ({type(error).__name__}). Проверь DATABASE_URL, доступность базы и права пользователя.\n")
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f"Не удалось прочитать SQLite ({type(error).__name__}). Проверь файл источника.\n")
    except ValueError as error:
        parser.exit(1, f"Ошибка переноса: {error}\n")
    if args.check:
        print(f"Настройки и PostgreSQL: OK. Часовой пояс: {settings.timezone.key}.")
        print("BOT_TOKEN: " + ("задан (не проверялся в Telegram)" if settings.token else "нужно заполнить .env"))
        print(f"Администраторов: {len(settings.admin_ids)}.")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service = BookingService(settings, store)
    application = build_application(settings, service)
    # Python 3.14+ не создаёт цикл автоматически при get_event_loop().
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        application.run_polling(allowed_updates=["message", "callback_query"], stop_signals=None)
    except TelegramError as error:
        parser.exit(1, f"Не удалось запустить Telegram-бота ({type(error).__name__}). Проверь токен и соединение.\n")
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()

