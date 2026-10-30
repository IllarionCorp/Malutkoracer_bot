import argparse
import asyncio
import logging

from telegram.error import TelegramError

from .config import Settings
from .handlers import build_application
from .service import BookingService
from .storage import BookingStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот бронирования гоночного трека")
    parser.add_argument("--check", action="store_true", help="Проверить настройки и SQLite без обращения к Telegram")
    args = parser.parse_args()
    try:
        settings = Settings.from_env(require_token=not args.check)
    except ValueError as error:
        parser.exit(1, f"Ошибка настройки: {error}\n")
    store = BookingStore(settings.database_path)
    store.initialize()
    if args.check:
        print(f"Настройки и SQLite: OK. Часовой пояс: {settings.timezone.key}.")
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

