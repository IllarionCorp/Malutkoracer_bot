from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from typing import Callable

from .config import Settings
from .storage import Availability, Booking, BookingError, BookingStore


def money(kopecks: int) -> str:
    rubles, remainder = divmod(kopecks, 100)
    amount = f"{rubles:,}".replace(",", " ")
    return f"{amount},{remainder:02d} ₽" if remainder else f"{amount} ₽"


def parse_price(value: str) -> int:
    normalized = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,2})?", normalized):
        raise BookingError("Укажи цену часа в рублях, например 2500 или 2500,50.")
    kopecks = int(Decimal(normalized) * 100)
    if kopecks <= 0:
        raise BookingError("Цена должна быть больше нуля.")
    return kopecks


def normalize_plate(value: str) -> str:
    plate = re.sub(r"[\s-]+", "", value.strip().upper())
    if not re.fullmatch(r"[A-ZА-ЯЁ0-9]{4,16}", plate) or not any(char.isdigit() for char in plate):
        raise BookingError("Укажи госномер автомобиля, например А123ВС77. Допустимы буквы и цифры, от 4 до 16 символов.")
    return plate


@dataclass(frozen=True)
class HourAvailability:
    starts_at: int
    remaining: int


class BookingService:
    def __init__(self, settings: Settings, store: BookingStore, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.store = store
        self.clock = clock or (lambda: datetime.now(settings.timezone))

    def now(self) -> datetime:
        return self.clock().astimezone(self.settings.timezone)

    def parse_day(self, value: str) -> date:
        day = None
        for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(value.strip(), pattern).date()
                break
            except ValueError:
                continue
        if day is None:
            raise BookingError("Укажи дату в формате ДД.ММ.ГГГГ, например 30.10.2026.")
        today = self.now().date()
        if not today <= day <= today + timedelta(days=self.settings.booking_days):
            raise BookingError(f"Выбери дату от сегодня до {self.settings.booking_days} дней вперёд.")
        return day

    def parse_times(self, day: date, value: str) -> tuple[int, int]:
        match = re.fullmatch(r"([0-9]{2}:[0-9]{2})\s*[-–—]\s*([0-9]{2}:[0-9]{2})", value.strip())
        try:
            if not match:
                raise ValueError
            parts = match.groups()
            start = datetime.combine(day, time.fromisoformat(parts[0]), self.settings.timezone)
            end = (datetime.combine(day + timedelta(days=1), time.min, self.settings.timezone)
                   if parts[1] == "24:00" else datetime.combine(day, time.fromisoformat(parts[1]), self.settings.timezone))
            if datetime.fromtimestamp(start.timestamp(), self.settings.timezone) != start:
                raise ValueError
            if datetime.fromtimestamp(end.timestamp(), self.settings.timezone) != end:
                raise ValueError
        except (ValueError, TypeError):
            raise BookingError("Укажи время в формате 10:00-20:00 (окончание может быть 24:00).") from None
        if start <= self.now() or end.timestamp() - start.timestamp() < 3600:
            raise BookingError("Начало должно быть в будущем, а окно — длиться не меньше часа.")
        return int(start.timestamp()), int(end.timestamp())

    def window(self, window_id: int) -> Availability:
        window = self.store.get_window(window_id)
        if window is None or window.status != "open":
            raise BookingError("Это окно закрыто. Выбери другое время: /book.")
        return window

    def hour_availability(self, window: Availability) -> list[HourAvailability]:
        earliest = int(self.now().timestamp()) + self.settings.min_notice_minutes * 60
        return [HourAvailability(start, self.store.remaining(start, start + 3600, window.capacity))
                for start in range(window.starts_at, window.ends_at - 3599, 3600) if start > earliest]

    def starts(self, window: Availability) -> list[int]:
        return [hour.starts_at for hour in self.hour_availability(window) if hour.remaining > 0]

    def durations(self, window_id: int, starts_at: int) -> list[int]:
        window = self.window(window_id)
        earliest = int(self.now().timestamp()) + self.settings.min_notice_minutes * 60
        if starts_at <= earliest or starts_at < window.starts_at or (starts_at - window.starts_at) % 3600:
            return []
        maximum = min(self.settings.max_booking_hours, (window.ends_at - starts_at) // 3600)
        return [hours for hours in range(1, maximum + 1)
                if self.store.available(starts_at, starts_at + hours * 3600, window.capacity)]

    def book(self, *, window_id: int, starts_at: int, hours: int, user_id: int, full_name: str,
             username: str | None, car_plate: str, expected_price: int, request_key: str) -> Booking:
        if hours not in self.durations(window_id, starts_at):
            raise BookingError("На выбранную длительность свободного времени уже нет. Начни снова: /book.")
        return self.store.create(
            window_id=window_id, user_id=user_id, full_name=full_name[:256], username=username,
            car_plate=normalize_plate(car_plate), starts_at=starts_at, hours=hours,
            expected_price=expected_price, now=int(self.now().timestamp()), request_key=request_key,
        )
