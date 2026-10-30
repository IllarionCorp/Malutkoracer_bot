from dataclasses import dataclass, field
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name}: укажи целое число.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: допустимо от {minimum} до {maximum}.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    track_name: str
    timezone: ZoneInfo
    admin_ids: frozenset[int]
    database_url: str = field(repr=False)
    max_booking_hours: int = 24
    booking_days: int = 365
    min_notice_minutes: int = 0

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Settings":
        load_dotenv(ROOT / ".env", encoding="utf-8-sig")
        token = os.getenv("BOT_TOKEN", "").strip()
        if require_token and (not token or ":" not in token):
            raise ValueError("Впиши BOT_TOKEN от @BotFather в файл .env.")
        track_name = os.getenv("TRACK_NAME", "Гоночный трек").strip()
        if not 1 <= len(track_name) <= 80:
            raise ValueError("TRACK_NAME: название должно содержать от 1 до 80 символов.")
        try:
            timezone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TIMEZONE: неизвестный часовой пояс. Например: Europe/Moscow.") from None
        try:
            admin_ids = frozenset(int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip())
        except ValueError:
            raise ValueError("ADMIN_IDS: укажи числовые ID через запятую.") from None
        if any(user_id <= 0 for user_id in admin_ids):
            raise ValueError("ADMIN_IDS: нужны положительные ID пользователей.")
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL: укажи строку подключения postgresql://user:password@host:5432/database в .env.")
        return cls(
            token=token, track_name=track_name, timezone=timezone, admin_ids=admin_ids,
            database_url=database_url,
            max_booking_hours=_integer("MAX_BOOKING_HOURS", 24, 1, 24),
            booking_days=_integer("BOOKING_DAYS", 365, 1, 730),
            min_notice_minutes=_integer("MIN_NOTICE_MINUTES", 0, 0, 43_200),
        )

