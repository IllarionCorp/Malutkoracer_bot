from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator


class BookingError(Exception):
    """Ожидаемая ошибка, текст которой можно показать пользователю."""


@dataclass(frozen=True)
class Availability:
    id: int
    starts_at: int
    ends_at: int
    hourly_price: int  # Копейки: расчёт денег без float.
    status: str
    capacity: int = 1

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Availability":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Booking:
    id: int
    window_id: int
    user_id: int
    full_name: str
    username: str | None
    car_plate: str
    starts_at: int
    ends_at: int
    hours: int
    hourly_price: int
    total_price: int
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Booking":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Participant:
    full_name: str
    username: str | None
    starts_at: int
    ends_at: int
    hours: int


class BookingStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS availability (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
                    hourly_price INTEGER NOT NULL CHECK (hourly_price > 0),
                    capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity BETWEEN 1 AND 1000),
                    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id INTEGER NOT NULL REFERENCES availability(id),
                    user_id INTEGER NOT NULL,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    car_plate TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
                    hours INTEGER NOT NULL CHECK (hours BETWEEN 1 AND 24),
                    hourly_price INTEGER NOT NULL CHECK (hourly_price > 0),
                    total_price INTEGER NOT NULL CHECK (total_price = hourly_price * hours),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
                    request_key TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    cancelled_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS availability_time ON availability(starts_at, ends_at);
                CREATE INDEX IF NOT EXISTS bookings_active_time
                    ON bookings(starts_at, ends_at) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS bookings_user ON bookings(user_id, starts_at);
            """)
            # Старые окна сохраняют прежнюю вместимость — один автомобиль.
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(availability)")}
            if "capacity" not in columns:
                connection.execute(
                    "ALTER TABLE availability ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (capacity BETWEEN 1 AND 1000)"
                )
            connection.commit()

    @staticmethod
    def _remaining(connection: sqlite3.Connection, starts_at: int, ends_at: int, capacity: int) -> int:
        bookings = connection.execute(
            "SELECT starts_at, ends_at FROM bookings WHERE status = 'active' AND starts_at < ? AND ends_at > ?",
            (ends_at, starts_at),
        ).fetchall()
        events = []
        for booking in bookings:
            events.append((max(starts_at, booking["starts_at"]), 1))
            events.append((min(ends_at, booking["ends_at"]), -1))
        occupied = peak = 0
        # Освобождение места в 12:00 учитывается раньше новой брони с 12:00.
        for _, change in sorted(events):
            occupied += change
            peak = max(peak, occupied)
        return max(0, capacity - peak)

    def add_window(self, *, starts_at: int, ends_at: int, hourly_price: int, admin_id: int, now: int,
                   capacity: int = 1) -> Availability:
        if starts_at <= now or ends_at - starts_at < 3600 or hourly_price <= 0:
            raise BookingError("Укажи будущее окно не короче часа и положительную цену.")
        if not isinstance(capacity, int) or not 1 <= capacity <= 1000:
            raise BookingError("Количество мест должно быть целым числом от 1 до 1000.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            overlap = connection.execute(
                "SELECT 1 FROM availability WHERE status = 'open' AND starts_at < ? AND ends_at > ? LIMIT 1",
                (ends_at, starts_at),
            ).fetchone()
            if overlap:
                raise BookingError("На это время уже есть окно. Посмотри /slots или выбери другое время.")
            cursor = connection.execute(
                "INSERT INTO availability (starts_at, ends_at, hourly_price, created_by, created_at, capacity) VALUES (?, ?, ?, ?, ?, ?)",
                (starts_at, ends_at, hourly_price, admin_id, now, capacity),
            )
            row = connection.execute("SELECT * FROM availability WHERE id = ?", (cursor.lastrowid,)).fetchone()
            connection.commit()
            return Availability.from_row(row)

    def get_window(self, window_id: int) -> Availability | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM availability WHERE id = ?", (window_id,)).fetchone()
            return Availability.from_row(row) if row else None

    def windows(self, now: int, *, include_closed: bool = False) -> list[Availability]:
        status_filter = "status = 'open'"
        if include_closed:
            status_filter = """(status = 'open' OR EXISTS (
                SELECT 1 FROM bookings b WHERE b.window_id = availability.id
                    AND b.status = 'active' AND b.ends_at > ?
            ))"""
        parameters = (now, now) if include_closed else (now,)
        with self.connect() as connection:
            return [Availability.from_row(row) for row in connection.execute(
                f"SELECT * FROM availability WHERE {status_filter} AND ends_at > ? ORDER BY starts_at, id", parameters,
            )]

    def close_window(self, window_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("UPDATE availability SET status = 'closed' WHERE id = ? AND status = 'open'", (window_id,))
            return cursor.rowcount == 1

    def remaining(self, starts_at: int, ends_at: int, capacity: int) -> int:
        with self.connect() as connection:
            return self._remaining(connection, starts_at, ends_at, capacity)

    def available(self, starts_at: int, ends_at: int, capacity: int = 1) -> bool:
        return self.remaining(starts_at, ends_at, capacity) > 0

    def participants(self, starts_at: int, ends_at: int, *, limit: int = 5, offset: int = 0) -> list[Participant]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT full_name, username, starts_at, ends_at, hours FROM bookings
                   WHERE status = 'active' AND starts_at < ? AND ends_at > ?
                   ORDER BY starts_at, id LIMIT ? OFFSET ?""",
                (ends_at, starts_at, limit, offset),
            )
            return [Participant(**dict(row)) for row in rows]

    def create(
        self, *, window_id: int, user_id: int, full_name: str, username: str | None,
        car_plate: str, starts_at: int, hours: int, expected_price: int, now: int,
        request_key: str,
    ) -> Booking:
        with self.connect() as connection:
            # Проверяем окно и пересечения и записываем бронь под одной блокировкой.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM bookings WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                if existing["user_id"] != user_id or existing["status"] != "active":
                    raise BookingError("Это подтверждение уже использовано. Начни новую запись: /book.")
                connection.commit()
                return Booking.from_row(existing)
            row = connection.execute("SELECT * FROM availability WHERE id = ? AND status = 'open'", (window_id,)).fetchone()
            if not row:
                raise BookingError("Администратор закрыл это время для записи. Выбери другое: /book.")
            ends_at = starts_at + hours * 3600
            if (not 1 <= hours <= 24 or starts_at <= now or starts_at < row["starts_at"]
                    or ends_at > row["ends_at"] or (starts_at - row["starts_at"]) % 3600):
                raise BookingError("Выбранное время недоступно. Начни новую запись: /book.")
            if row["hourly_price"] * hours != expected_price:
                raise BookingError("Стоимость изменилась. Начни новую запись: /book.")
            if self._remaining(connection, starts_at, ends_at, row["capacity"]) == 0:
                raise BookingError("На это время свободных мест уже нет. Выбери другой сеанс: /book.")
            cursor = connection.execute(
                """INSERT INTO bookings
                   (window_id, user_id, full_name, username, car_plate, starts_at, ends_at,
                    hours, hourly_price, total_price, request_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (window_id, user_id, full_name, username, car_plate, starts_at, ends_at,
                 hours, row["hourly_price"], expected_price, request_key, now),
            )
            booking = connection.execute("SELECT * FROM bookings WHERE id = ?", (cursor.lastrowid,)).fetchone()
            connection.commit()
            return Booking.from_row(booking)

    def upcoming(self, now: int, *, user_id: int | None = None, limit: int = 10, offset: int = 0) -> list[Booking]:
        query = "SELECT * FROM bookings WHERE status = 'active' AND ends_at > ?"
        parameters = [now]
        if user_id is not None:
            query += " AND user_id = ?"
            parameters.append(user_id)
        query += " ORDER BY starts_at, id LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self.connect() as connection:
            return [Booking.from_row(row) for row in connection.execute(query, parameters)]

    def get_owned(self, booking_id: int, user_id: int) -> Booking | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user_id)).fetchone()
            return Booking.from_row(row) if row else None

    def cancel(self, booking_id: int, user_id: int, now: int) -> Booking:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user_id)).fetchone()
            if not row or row["status"] != "active":
                raise BookingError("Бронь не найдена или уже отменена. Посмотри /my.")
            if row["starts_at"] <= now:
                raise BookingError("Сеанс уже начался. Для отмены свяжись с администратором.")
            connection.execute("UPDATE bookings SET status = 'cancelled', cancelled_at = ? WHERE id = ?", (now, booking_id))
            updated = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            connection.commit()
            return Booking.from_row(updated)

