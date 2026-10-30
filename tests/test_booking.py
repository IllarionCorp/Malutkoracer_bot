from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from zoneinfo import ZoneInfo

from booking_bot.config import Settings
from booking_bot.service import BookingService, money, normalize_plate, parse_price
from booking_bot.storage import BookingError, BookingStore


class BookingFixture:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2030, 5, 10, 9, tzinfo=ZoneInfo("Europe/Moscow"))
        self.settings = Settings(
            token="123456:offline_test_token", track_name="Тестовый трек", timezone=self.now.tzinfo,
            admin_ids=frozenset({1}), database_path=Path(self.directory.name) / "bookings.sqlite3",
        )
        self.store = BookingStore(self.settings.database_path)
        self.store.initialize()
        self.service = BookingService(self.settings, self.store, clock=lambda: self.now)

    def timestamp(self, hour: int, minute: int = 0) -> int:
        return int(self.now.replace(hour=hour, minute=minute).timestamp())

    def window(self, start: int = 10, end: int = 20, price: int = 250_050, *, capacity: int = 1):
        return self.store.add_window(starts_at=self.timestamp(start), ends_at=self.timestamp(end),
                                     hourly_price=price, admin_id=1, now=int(self.now.timestamp()), capacity=capacity)

    def book(self, window_id: int, *, hour: int = 10, hours: int = 1, user_id: int = 20,
             key: str = "test", total: int | None = None, username: str | None = "driver", full_name: str = "Пилот"):
        window = self.store.get_window(window_id)
        return self.service.book(
            window_id=window_id, starts_at=self.timestamp(hour), hours=hours, user_id=user_id,
            full_name=full_name, username=username, car_plate="а 123 вс-77",
            expected_price=window.hourly_price * hours if total is None else total, request_key=key,
        )


class BookingTests(BookingFixture, unittest.TestCase):
    def test_multihour_total_and_persistence(self):
        window = self.window()
        booking = self.book(window.id, hours=3)
        self.assertEqual(booking.total_price, 750_150)
        self.assertEqual(booking.ends_at - booking.starts_at, 3 * 3600)
        self.assertEqual(booking.car_plate, "А123ВС77")
        reopened = BookingStore(self.settings.database_path)
        reopened.initialize()
        self.assertEqual(reopened.upcoming(int(self.now.timestamp()))[0], booking)

    def test_price_uses_kopecks(self):
        self.assertEqual(parse_price("2500,50"), 250_050)
        self.assertEqual(parse_price("0.01"), 1)
        self.assertEqual(money(750_150), "7 501,50 ₽")
        self.assertEqual(money(750_000), "7 500 ₽")

    def test_invalid_prices(self):
        for value in ("0", "-5", "NaN", "Infinity", "10.001", "1e3", "abc"):
            with self.subTest(value=value), self.assertRaises(BookingError):
                parse_price(value)

    def test_plate_validation(self):
        self.assertEqual(normalize_plate(" а 123 вс-77 "), "А123ВС77")
        for value in ("abc", "123", "AAAA", "А123<script>", "x" * 17):
            with self.subTest(value=value), self.assertRaises(BookingError):
                normalize_plate(value)

    def test_overlapping_booking_rejected(self):
        window = self.window()
        self.book(window.id, hours=3)
        with self.assertRaises(BookingError):
            self.book(window.id, hour=12, hours=2, user_id=21, key="other")
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 1)

    def test_adjacent_bookings_allowed(self):
        window = self.window()
        first = self.book(window.id)
        second = self.book(window.id, hour=11, key="second", user_id=21)
        self.assertEqual(first.ends_at, second.starts_at)

    def test_durations_stop_at_next_booking(self):
        window = self.window()
        self.book(window.id, hour=12, hours=2)
        self.assertEqual(self.service.durations(window.id, self.timestamp(11)), [1])
        self.assertNotIn(self.timestamp(12), self.service.starts(window))

    def test_booking_cannot_exceed_window(self):
        window = self.window(end=12)
        with self.assertRaises(BookingError):
            self.book(window.id, hour=11, hours=2)

    def test_off_grid_start_rejected(self):
        window = self.window()
        self.assertEqual(self.service.durations(window.id, self.timestamp(10, 30)), [])

    def test_past_start_rejected(self):
        window = self.window()
        self.now = self.now.replace(hour=11)
        with self.assertRaises(BookingError):
            self.book(window.id, hour=10)

    def test_forged_price_rejected(self):
        window = self.window()
        with self.assertRaises(BookingError):
            self.book(window.id, total=1)

    def test_close_window_preserves_existing_bookings(self):
        window = self.window()
        booking = self.book(window.id)
        self.assertTrue(self.store.close_window(window.id))
        self.assertEqual(self.store.windows(int(self.now.timestamp())), [])
        self.assertEqual(self.store.get_owned(booking.id, booking.user_id), booking)
        with self.assertRaises(BookingError):
            self.book(window.id, hour=12, key="new")

    def test_closed_window_booking_still_blocks_new_window(self):
        window = self.window()
        self.book(window.id, hours=2)
        self.store.close_window(window.id)
        replacement = self.window()
        with self.assertRaises(BookingError):
            self.book(replacement.id, hour=11, user_id=21, key="new")

    def test_owner_can_cancel_and_time_is_released(self):
        window = self.window()
        booking = self.book(window.id, hours=2)
        cancelled = self.store.cancel(booking.id, booking.user_id, int(self.now.timestamp()))
        self.assertEqual(cancelled.status, "cancelled")
        new_booking = self.book(window.id, hours=2, key="new", user_id=21)
        self.assertNotEqual(new_booking.id, booking.id)

    def test_other_user_cannot_cancel(self):
        window = self.window()
        booking = self.book(window.id)
        with self.assertRaises(BookingError):
            self.store.cancel(booking.id, 999, int(self.now.timestamp()))
        self.assertEqual(self.store.get_owned(booking.id, booking.user_id).status, "active")

    def test_started_booking_cannot_be_cancelled(self):
        window = self.window()
        booking = self.book(window.id)
        with self.assertRaises(BookingError):
            self.store.cancel(booking.id, booking.user_id, self.timestamp(10))

    def test_repeated_storage_confirmation_is_idempotent(self):
        window = self.window()
        booking = self.book(window.id)
        repeated = self.store.create(
            window_id=window.id, user_id=booking.user_id, full_name=booking.full_name,
            username=booking.username, car_plate=booking.car_plate, starts_at=booking.starts_at,
            hours=booking.hours, expected_price=booking.total_price, now=int(self.now.timestamp()), request_key="test",
        )
        self.assertEqual(repeated.id, booking.id)
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 1)

    def test_cancelled_confirmation_cannot_be_reused(self):
        window = self.window()
        booking = self.book(window.id)
        self.store.cancel(booking.id, booking.user_id, int(self.now.timestamp()))
        with self.assertRaises(BookingError):
            self.book(window.id)

    def test_concurrent_overlapping_requests_have_one_winner(self):
        window = self.window()
        barrier = Barrier(2)

        def attempt(index):
            barrier.wait(timeout=5)
            try:
                return self.book(window.id, hour=10 + index, hours=2, key=str(index), user_id=20 + index)
            except BookingError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 1)

    def test_overlapping_admin_windows_rejected(self):
        self.window()
        with self.assertRaises(BookingError):
            self.window(start=11, end=21)

    def test_parse_date_and_times(self):
        day = self.service.parse_day("10.05.2030")
        self.assertEqual(day, self.now.date())
        start, end = self.service.parse_times(day, "10:00-24:00")
        self.assertEqual(end - start, 14 * 3600)
        self.assertEqual(self.service.parse_day("2030-05-10"), day)

    def test_invalid_date_and_times(self):
        for value in ("31.02.2030", "yesterday", "09.05.2030"):
            with self.subTest(value=value), self.assertRaises(BookingError):
                self.service.parse_day(value)
        for value in ("12:00-10:00", "10:00-10:30", "08:00-12:00", "10:00-25:00", "10:00+03:00-20:00"):
            with self.subTest(value=value), self.assertRaises(BookingError):
                self.service.parse_times(self.now.date(), value)

    def test_windows_allow_half_hour_start(self):
        start, end = self.service.parse_times(self.now.date(), "10:30-15:30")
        window = self.store.add_window(starts_at=start, ends_at=end, hourly_price=100_000,
                                      admin_id=1, now=int(self.now.timestamp()))
        self.assertEqual(self.service.starts(window)[0], self.timestamp(10, 30))
        self.assertEqual(self.service.durations(window.id, start), [1, 2, 3, 4, 5])

    def test_capacity_allows_multiple_cars_and_rejects_excess(self):
        window = self.window(capacity=2)
        self.book(window.id, hours=2)
        self.book(window.id, hours=2, user_id=21, key="second")
        with self.assertRaises(BookingError):
            self.book(window.id, hours=2, user_id=22, key="third")
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 2)

    def test_remaining_places_by_hour(self):
        window = self.window(end=14, capacity=3)
        self.book(window.id, hours=2)
        self.book(window.id, hour=11, hours=2, user_id=21, key="second")
        self.assertEqual([hour.remaining for hour in self.service.hour_availability(window)], [2, 1, 2, 3])
        self.assertEqual(self.store.remaining(self.timestamp(10), self.timestamp(13), 3), 1)

    def test_capacity_uses_peak_not_total_overlaps(self):
        window = self.window(capacity=2)
        self.book(window.id, hour=10, hours=1)
        self.book(window.id, hour=11, hours=1, user_id=21, key="second")
        self.assertEqual(self.store.remaining(self.timestamp(10), self.timestamp(12), 2), 1)
        long_booking = self.book(window.id, hours=2, user_id=22, key="third")
        self.assertEqual(long_booking.hours, 2)

    def test_capacity_cannot_be_exceeded_by_concurrent_requests(self):
        window = self.window(capacity=2)
        barrier = Barrier(3)

        def attempt(index):
            barrier.wait(timeout=5)
            try:
                return self.book(window.id, hours=2, key=str(index), user_id=20 + index)
            except BookingError:
                return None

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(attempt, range(3)))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(len(self.store.upcoming(int(self.now.timestamp()))), 2)

    def test_cancellation_releases_one_place_and_removes_participant(self):
        window = self.window(capacity=2)
        booking = self.book(window.id, username="first")
        self.book(window.id, user_id=21, key="second", username="second")
        self.store.cancel(booking.id, booking.user_id, int(self.now.timestamp()))
        self.assertEqual(self.store.remaining(self.timestamp(10), self.timestamp(11), 2), 1)
        people = self.store.participants(window.starts_at, window.ends_at)
        self.assertEqual([person.username for person in people], ["second"])
        self.assertFalse(hasattr(people[0], "car_plate"))

    def test_capacity_includes_bookings_from_closed_windows(self):
        original = self.window(capacity=2)
        self.book(original.id)
        self.book(original.id, user_id=21, key="second")
        self.store.close_window(original.id)
        replacement = self.window(capacity=3)
        self.assertEqual(self.service.hour_availability(replacement)[0].remaining, 1)
        self.book(replacement.id, user_id=22, key="third")
        with self.assertRaises(BookingError):
            self.book(replacement.id, user_id=23, key="fourth")

    def test_invalid_capacities_rejected(self):
        for capacity in (0, -1, 1001, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(BookingError):
                self.window(capacity=capacity)

    def test_existing_database_migrates_without_losing_booking(self):
        window = self.window()
        booking = self.book(window.id, hours=3)
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        with self.store.connect() as source, closing(sqlite3.connect(legacy_path)) as target:
            target.execute("""CREATE TABLE availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT, starts_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL, hourly_price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            row = source.execute("SELECT id, starts_at, ends_at, hourly_price, status, created_by, created_at FROM availability").fetchone()
            target.execute("INSERT INTO availability VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(row))
            schema = source.execute("SELECT sql FROM sqlite_master WHERE name = 'bookings'").fetchone()[0]
            target.execute(schema)
            booking_row = source.execute("SELECT * FROM bookings").fetchone()
            placeholders = ", ".join("?" for _ in booking_row)
            target.execute(f"INSERT INTO bookings VALUES ({placeholders})", tuple(booking_row))
            target.commit()
        migrated = BookingStore(legacy_path)
        migrated.initialize()
        migrated.initialize()
        self.assertEqual(migrated.get_window(window.id).capacity, 1)
        self.assertEqual(migrated.get_owned(booking.id, booking.user_id), booking)


if __name__ == "__main__":
    unittest.main()

